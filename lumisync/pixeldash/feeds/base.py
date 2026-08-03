"""Feed interface plus a dependency-free JSON-over-HTTPS client.

``urllib`` is used deliberately: LumiSync already ships without an HTTP library
and a dashboard that polls two REST endpoints does not justify adding one. The
client honours the standard proxy environment variables through
``urllib.request``'s default opener.
"""

from __future__ import annotations

import abc
import datetime as _dt
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ..models import AgentReport, DataClass, DayStats, FeedStatus, Position, Trade

DEFAULT_TIMEOUT = 8.0


class FeedError(RuntimeError):
    """The upstream feed was reachable-in-principle but did not deliver data.

    Callers turn this into a :class:`FeedStatus` in the ``error`` state and
    render it. It is never swallowed into empty results — an empty account and
    a broken connection must not look the same.
    """


class FeedUnconfigured(FeedError):
    """Credentials for this feed are missing or incomplete."""


def _decode(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FeedError(f"malformed JSON response: {exc}") from exc


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    opener=None,
) -> Any:
    """GET a URL and parse JSON, raising :class:`FeedError` on any failure.

    ``opener`` exists so tests can drive the feeds without a network; it takes
    the same ``(request, timeout)`` arguments as ``urllib.request.urlopen``.
    """
    if params:
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value not in (None, "")}
        )
        url = f"{url}?{query}" if query else url

    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    fetch = opener or urllib.request.urlopen
    try:
        with fetch(request, timeout=timeout) as response:
            return _decode(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.reason or ""
        if exc.code in (401, 403):
            raise FeedUnconfigured(f"HTTP {exc.code} — check credentials") from exc
        raise FeedError(f"HTTP {exc.code} {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise FeedError(f"unreachable: {exc.reason}") from exc
    except FeedError:
        raise
    except Exception as exc:  # socket timeouts, TLS failures, …
        raise FeedError(f"{type(exc).__name__}: {exc}") from exc


def as_list(value: Any, key: str = "") -> List[Any]:
    """Normalise the many shapes brokers use for "zero, one, or many".

    Tradier in particular returns the string ``"null"`` for empty collections
    and a bare object (not a list) when exactly one item exists.
    """
    if key:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if value in (None, "null", ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def to_float(value: Any) -> Optional[float]:
    """Parse a number that may arrive as a string, or None if it is absent.

    Returns ``None`` rather than ``0.0`` for unparseable input so callers can
    tell "the broker didn't say" from "the broker said zero".
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> Optional[_dt.datetime]:
    """Parse the ISO-8601 shapes the supported brokers emit."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --- OCC option symbols -------------------------------------------------
#
# Both brokers hand back option positions using the 21-character OCC symbol
# (root, yymmdd, C/P, 8-digit strike x1000). Parsing it locally means the
# underlying and the x100 multiplier never have to be guessed.

def parse_occ_symbol(symbol: str) -> Optional[Tuple[str, _dt.date, str, float]]:
    """Split an OCC option symbol into ``(root, expiry, right, strike)``.

    Returns ``None`` for anything that is not an option symbol.
    """
    text = (symbol or "").strip().upper()
    if len(text) < 16:
        return None
    tail = text[-15:]
    root = text[:-15].strip()
    if not root or not root.isalpha():
        return None
    date_part, right, strike_part = tail[:6], tail[6], tail[7:]
    if right not in ("C", "P") or not date_part.isdigit() or not strike_part.isdigit():
        return None
    try:
        expiry = _dt.date(2000 + int(date_part[:2]), int(date_part[2:4]), int(date_part[4:6]))
    except ValueError:
        return None
    return root, expiry, right, int(strike_part) / 1000.0


def contract_multiplier(symbol: str) -> int:
    """100 for an option symbol, 1 for anything else."""
    return 100 if parse_occ_symbol(symbol) else 1


def underlying_of(symbol: str) -> str:
    parsed = parse_occ_symbol(symbol)
    return parsed[0] if parsed else (symbol or "").strip().upper()


class TradeFeed(abc.ABC):
    """One broker connection.

    Implementations raise :class:`FeedError` on failure and return real data
    otherwise. They never return partially-invented results; if positions load
    but marks do not, the marks come back as ``None`` and the renderer shows
    that gap rather than a plausible-looking substitute.
    """

    #: Short, stable identifier used in status lines and on the panel.
    name: str = "feed"

    @property
    @abc.abstractmethod
    def data_class(self) -> DataClass:
        """Whether this feed carries live or paper money."""

    @abc.abstractmethod
    def fetch_positions(self) -> List[Position]:
        """Currently open positions, with marks where the broker provides them."""

    @abc.abstractmethod
    def fetch_trades(self, since: _dt.date, until: _dt.date) -> List[Trade]:
        """Closed round trips in ``[since, until]`` (inclusive, exchange-local)."""

    def fetch_equity(self) -> Optional[float]:
        """Account equity, or None when the broker does not expose it."""
        return None

    def fetch_buying_power(self) -> Optional[float]:
        return None

    def account_label(self) -> str:
        return self.name

    def agent_report(self, state: str, message: str) -> AgentReport:
        """Describe this feed as a workflow agent for the agents scene."""
        return AgentReport(
            agent_id=self.name,
            label=self.name.upper()[:6],
            state=state,
            message=message,
            updated_at=_dt.datetime.now(),
        )

    def status_ok(self) -> FeedStatus:
        return FeedStatus(name=self.name, checked_at=_dt.datetime.now())


def days_from_trades(trades: List[Trade], data_class: DataClass) -> List[DayStats]:
    """Fold closed trades into per-date stats, split by RTH/ETH session.

    Only dates that actually traded appear. An untraded day is absent from the
    result — the calendar draws it as unlit rather than as a flat zero.
    """
    from ..models import Session

    buckets: Dict[_dt.date, Dict[str, float]] = {}
    for trade in trades:
        bucket = buckets.setdefault(
            trade.trade_date,
            {"realized": 0.0, "trades": 0, "wins": 0, "losses": 0, "rth": 0.0, "eth": 0.0},
        )
        bucket["realized"] += trade.realized
        bucket["trades"] += 1
        if trade.is_win:
            bucket["wins"] += 1
        elif trade.realized < 0:
            bucket["losses"] += 1
        if trade.session is Session.RTH:
            bucket["rth"] += trade.realized
        else:
            bucket["eth"] += trade.realized

    return [
        DayStats(
            date=date,
            realized=bucket["realized"],
            trades=int(bucket["trades"]),
            wins=int(bucket["wins"]),
            losses=int(bucket["losses"]),
            rth_realized=bucket["rth"],
            eth_realized=bucket["eth"],
            data_class=data_class,
        )
        for date, bucket in sorted(buckets.items())
    ]
