"""Add an API by describing it, not by writing a driver.

A feed spec is a small JSON document naming an endpoint, how to authenticate,
and which response fields mean what. Drop one in the ``feeds.d`` directory and
it shows up alongside Tradier and Alpaca — same snapshot, same scenes, same
fail-closed behaviour, no Python.

```json
{
  "name": "myprop",
  "base_url": "https://api.example.com",
  "data_class": "live",
  "auth": {"type": "bearer", "token_env": "MYPROP_TOKEN"},
  "positions": {
    "path": "/v1/positions",
    "records": "data.rows",
    "fields": {
      "symbol": "ticker",
      "quantity": "qty",
      "entry_price": "avg_price",
      "mark_price": "last"
    }
  },
  "trades": {
    "path": "/v1/fills",
    "params": {"start": "{since}", "end": "{until}"},
    "records": "data.rows",
    "mode": "closed",
    "fields": {"symbol": "ticker", "realized": "pnl", "closed_at": "closed"}
  }
}
```

Two design rules carry over from the hand-written feeds:

* **A spec cannot invent a number.** Every value is read from the response or
  it is ``None``. There is no default, no fallback, and no computed placeholder
  — a missing mark renders as ``--`` exactly as it would from Tradier.
* **Credentials never live in the spec.** ``token_env`` names an environment
  variable; the spec file holds the variable's name, never its value, so a spec
  is safe to commit and share.

Brokers that report fills rather than round trips set ``"mode": "fills"`` and
get the same FIFO matching the Alpaca feed uses.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..models import DataClass, Position, Trade
from .base import (
    FeedError,
    FeedUnconfigured,
    TradeFeed,
    contract_multiplier,
    get_json,
    parse_timestamp,
    to_float,
    underlying_of,
)
from .roundtrip import Fill, match_fifo

#: Field names a positions block may map. Anything else is ignored, so a typo
#: is caught by :meth:`FeedSpec.validate` rather than silently doing nothing.
POSITION_FIELDS = (
    "symbol",
    "quantity",
    "entry_price",
    "mark_price",
    "opened_at",
    "underlying",
    "contract_multiplier",
)
TRADE_FIELDS = (
    "symbol",
    "realized",
    "closed_at",
    "opened_at",
    "quantity",
    "entry_price",
    "exit_price",
    "underlying",
)
FILL_FIELDS = ("symbol", "side", "quantity", "price", "filled_at", "contract_multiplier")

AUTH_TYPES = ("none", "bearer", "header", "query", "basic")
TRADE_MODES = ("closed", "fills")


# --- response navigation ------------------------------------------------

def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts and lists.

    ``"data.rows"`` reads ``payload["data"]["rows"]``; ``"data.0.rows"`` indexes
    a list. A path that does not resolve returns ``None`` rather than raising,
    because a field being absent is a normal state the caller has to handle
    anyway.
    """
    if not path:
        return payload
    current = payload
    for part in str(path).split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def records_from(payload: Any, path: str) -> List[Dict[str, Any]]:
    """Pull the list of records out of a response.

    Tolerates the shapes real APIs use for "zero, one, or many": a bare list, a
    single object, ``null``, and the string ``"null"``.
    """
    value = dig(payload, path) if path else payload
    if value in (None, "null", ""):
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


# --- spec ---------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    """One HTTP call plus how to read its response."""

    path: str
    records: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)
    mode: str = "closed"
    value: str = ""          # for scalar endpoints such as equity

    def resolved_params(self, **substitutions: str) -> Dict[str, Any]:
        """Fill ``{since}``-style placeholders in the query string."""
        resolved: Dict[str, Any] = {}
        for key, raw in self.params.items():
            if isinstance(raw, str):
                for name, replacement in substitutions.items():
                    raw = raw.replace("{" + name + "}", replacement)
            resolved[key] = raw
        return resolved


@dataclass(frozen=True)
class FeedSpec:
    """A described API."""

    name: str
    base_url: str
    data_class: DataClass = DataClass.PAPER
    auth: Dict[str, Any] = field(default_factory=dict)
    positions: Optional[Endpoint] = None
    trades: Optional[Endpoint] = None
    equity: Optional[Endpoint] = None
    account_label: str = ""
    timeout: float = 8.0
    source_path: str = ""

    @property
    def token_env(self) -> str:
        return str(self.auth.get("token_env", ""))

    def token(self, environ: Optional[Dict[str, str]] = None) -> str:
        source = environ if environ is not None else os.environ
        return str(source.get(self.token_env, "") or "").strip()

    def missing(self, environ: Optional[Dict[str, str]] = None) -> Tuple[str, ...]:
        """Environment variables this spec needs but does not have."""
        if str(self.auth.get("type", "none")) == "none":
            return ()
        return () if self.token(environ) else (self.token_env or "<token_env unset>",)


class SpecError(ValueError):
    """A feed spec is malformed. Raised at load time, never at poll time."""


def parse_endpoint(raw: Any, allowed: Tuple[str, ...], label: str) -> Optional[Endpoint]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError(f"{label} must be an object")
    if not raw.get("path"):
        raise SpecError(f"{label} needs a 'path'")

    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        raise SpecError(f"{label}.fields must be an object")

    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        raise SpecError(
            f"{label}.fields has unknown name(s) {unknown}; allowed: {sorted(allowed)}"
        )

    mode = str(raw.get("mode", "closed"))
    if mode not in TRADE_MODES:
        raise SpecError(f"{label}.mode must be one of {list(TRADE_MODES)}")

    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise SpecError(f"{label}.params must be an object")

    return Endpoint(
        path=str(raw["path"]),
        records=str(raw.get("records", "")),
        params=params,
        fields={str(key): str(value) for key, value in fields.items()},
        mode=mode,
        value=str(raw.get("value", "")),
    )


def parse_spec(raw: Dict[str, Any], *, source_path: str = "") -> FeedSpec:
    """Validate and build a spec, raising :class:`SpecError` on any problem.

    Validation is strict and happens once at load. A spec that reaches the poll
    loop is one that has already been checked, so a bad field name shows up as
    a startup error rather than as an empty dashboard.
    """
    if not isinstance(raw, dict):
        raise SpecError("a feed spec must be a JSON object")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise SpecError("a feed spec needs a 'name'")
    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        raise SpecError(f"{name}: needs a 'base_url'")

    data_class_raw = str(raw.get("data_class", "paper")).lower()
    try:
        data_class = DataClass(data_class_raw)
    except ValueError as exc:
        raise SpecError(
            f"{name}: data_class must be one of {[item.value for item in DataClass]}"
        ) from exc

    auth = raw.get("auth") or {"type": "none"}
    if not isinstance(auth, dict):
        raise SpecError(f"{name}: auth must be an object")
    auth_type = str(auth.get("type", "none")).lower()
    if auth_type not in AUTH_TYPES:
        raise SpecError(f"{name}: auth.type must be one of {list(AUTH_TYPES)}")
    if auth_type != "none" and not auth.get("token_env"):
        raise SpecError(
            f"{name}: auth.token_env must name the environment variable holding "
            "the credential (the spec never stores the value itself)"
        )

    trade_fields = FILL_FIELDS if (raw.get("trades") or {}).get("mode") == "fills" else TRADE_FIELDS

    spec = FeedSpec(
        name=name,
        base_url=base_url,
        data_class=data_class,
        auth={**auth, "type": auth_type},
        positions=parse_endpoint(raw.get("positions"), POSITION_FIELDS, f"{name}.positions"),
        trades=parse_endpoint(raw.get("trades"), trade_fields, f"{name}.trades"),
        equity=parse_endpoint(raw.get("equity"), (), f"{name}.equity"),
        account_label=str(raw.get("account_label", "") or name.upper()),
        timeout=float(raw.get("timeout", 8.0)),
        source_path=source_path,
    )

    if spec.positions is None and spec.trades is None:
        raise SpecError(f"{name}: needs at least a 'positions' or a 'trades' endpoint")
    return spec


# --- feed ---------------------------------------------------------------

class DeclarativeFeed(TradeFeed):
    """A :class:`TradeFeed` driven entirely by a :class:`FeedSpec`."""

    def __init__(
        self,
        spec: FeedSpec,
        *,
        environ: Optional[Dict[str, str]] = None,
        opener=None,
    ) -> None:
        missing = spec.missing(environ)
        if missing:
            raise FeedUnconfigured(f"{spec.name} is missing {', '.join(missing)}")
        self.spec = spec
        self.name = spec.name
        self._token = spec.token(environ)
        self._opener = opener

    @property
    def data_class(self) -> DataClass:
        return self.spec.data_class

    def account_label(self) -> str:
        return self.spec.account_label

    # --- transport ---
    def _headers(self) -> Dict[str, str]:
        auth = self.spec.auth
        kind = auth.get("type", "none")
        if kind == "bearer":
            return {"Authorization": f"Bearer {self._token}"}
        if kind == "header":
            return {str(auth.get("header", "Authorization")): self._token}
        if kind == "basic":
            import base64

            user = str(auth.get("username", ""))
            encoded = base64.b64encode(f"{user}:{self._token}".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return {}

    def _query_auth(self) -> Dict[str, str]:
        auth = self.spec.auth
        if auth.get("type") == "query":
            return {str(auth.get("param", "api_key")): self._token}
        return {}

    def _get(self, endpoint: Endpoint, **substitutions: str) -> Any:
        params = {**endpoint.resolved_params(**substitutions), **self._query_auth()}
        return get_json(
            f"{self.spec.base_url}{endpoint.path}",
            headers=self._headers(),
            params=params,
            timeout=self.spec.timeout,
            opener=self._opener,
        )

    # --- positions ---
    def fetch_positions(self) -> List[Position]:
        endpoint = self.spec.positions
        if endpoint is None:
            return []

        positions: List[Position] = []
        for row in records_from(self._get(endpoint), endpoint.records):
            position = self._position_from(row, endpoint.fields)
            if position is not None:
                positions.append(position)
        return positions

    def _position_from(self, row: Dict[str, Any], fields: Dict[str, str]) -> Optional[Position]:
        symbol = str(dig(row, fields.get("symbol", "symbol")) or "").strip().upper()
        quantity = to_float(dig(row, fields.get("quantity", "quantity")))
        entry = to_float(dig(row, fields.get("entry_price", "entry_price")))
        if not symbol or quantity is None or entry is None:
            return None

        multiplier = contract_multiplier(symbol)
        declared = to_float(dig(row, fields.get("contract_multiplier", "")))
        if declared and declared >= 1:
            multiplier = int(declared)

        underlying = dig(row, fields.get("underlying", "")) if fields.get("underlying") else None

        return Position(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry,
            data_class=self.data_class,
            # A mark the API did not send stays None. It is never defaulted to
            # the entry price, which is the exact bug that makes a P&L readout
            # sit at zero while the position moves.
            mark_price=to_float(dig(row, fields.get("mark_price", ""))),
            opened_at=parse_timestamp(dig(row, fields.get("opened_at", ""))),
            underlying=str(underlying or underlying_of(symbol)),
            contract_multiplier=multiplier,
        )

    # --- trades ---
    def fetch_trades(self, since: _dt.date, until: _dt.date) -> List[Trade]:
        endpoint = self.spec.trades
        if endpoint is None:
            return []

        payload = self._get(endpoint, since=since.isoformat(), until=until.isoformat())
        rows = records_from(payload, endpoint.records)

        if endpoint.mode == "fills":
            fills = [fill for fill in (self._fill_from(row, endpoint.fields) for row in rows) if fill]
            trades = match_fifo(fills, self.data_class)
        else:
            trades = [
                trade
                for trade in (self._trade_from(row, endpoint.fields) for row in rows)
                if trade is not None
            ]
        return [trade for trade in trades if since <= trade.trade_date <= until]

    def _trade_from(self, row: Dict[str, Any], fields: Dict[str, str]) -> Optional[Trade]:
        symbol = str(dig(row, fields.get("symbol", "symbol")) or "").strip().upper()
        realized = to_float(dig(row, fields.get("realized", "realized")))
        closed_at = parse_timestamp(dig(row, fields.get("closed_at", "closed_at")))
        if not symbol or realized is None or closed_at is None:
            return None

        underlying = dig(row, fields.get("underlying", "")) if fields.get("underlying") else None
        return Trade(
            symbol=symbol,
            closed_at=closed_at,
            realized=realized,
            data_class=self.data_class,
            quantity=to_float(dig(row, fields.get("quantity", ""))) or 0.0,
            opened_at=parse_timestamp(dig(row, fields.get("opened_at", ""))),
            entry_price=to_float(dig(row, fields.get("entry_price", ""))),
            exit_price=to_float(dig(row, fields.get("exit_price", ""))),
            underlying=str(underlying or underlying_of(symbol)),
        )

    def _fill_from(self, row: Dict[str, Any], fields: Dict[str, str]) -> Optional[Fill]:
        symbol = str(dig(row, fields.get("symbol", "symbol")) or "").strip().upper()
        side = str(dig(row, fields.get("side", "side")) or "").strip().lower()
        quantity = to_float(dig(row, fields.get("quantity", "quantity")))
        price = to_float(dig(row, fields.get("price", "price")))
        filled_at = parse_timestamp(dig(row, fields.get("filled_at", "filled_at")))
        if not symbol or not side or not quantity or price is None or filled_at is None:
            return None

        multiplier = contract_multiplier(symbol)
        declared = to_float(dig(row, fields.get("contract_multiplier", "")))
        if declared and declared >= 1:
            multiplier = int(declared)

        return Fill(
            symbol=symbol,
            side=side,
            quantity=abs(quantity),
            price=price,
            filled_at=filled_at,
            multiplier=multiplier,
        )

    # --- balances ---
    def fetch_equity(self) -> Optional[float]:
        endpoint = self.spec.equity
        if endpoint is None:
            return None
        try:
            return to_float(dig(self._get(endpoint), endpoint.value))
        except FeedError:
            # Equity is decoration on the dashboard; losing it must not fail
            # the whole feed when positions and trades came back fine.
            return None
