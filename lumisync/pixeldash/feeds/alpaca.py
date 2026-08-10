"""Alpaca feed — used for the paper sleeve.

Alpaca's positions payload already carries ``current_price``, so marks come for
free. It has no realized gain/loss endpoint, so closed trades are rebuilt from
the FILL activity stream with FIFO matching (see
:mod:`lumisync.pixeldash.feeds.roundtrip`). That reconstruction only ever uses
fills Alpaca actually reported.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from ..config import BrokerConfig
from ..models import DataClass, Position, Trade
from .base import (
    FeedUnconfigured,
    TradeFeed,
    contract_multiplier,
    get_json,
    parse_timestamp,
    to_float,
    underlying_of,
)
from .roundtrip import Fill, match_fifo

PAGE_SIZE = 100
MAX_PAGES = 20


class AlpacaFeed(TradeFeed):
    name = "alpaca"

    def __init__(self, config: BrokerConfig, *, opener=None, timeout: float = 8.0) -> None:
        if not config.configured:
            missing = ", ".join(config.missing) or "credentials"
            raise FeedUnconfigured(f"Alpaca is missing {missing}")
        self.config = config
        self._opener = opener
        self._timeout = timeout

    @property
    def data_class(self) -> DataClass:
        return self.config.data_class

    def account_label(self) -> str:
        return "ALPACA" if self.data_class is DataClass.LIVE else "ALPACA/P"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return get_json(
            f"{self.config.host}{path}",
            headers={
                "APCA-API-KEY-ID": self.config.token,
                "APCA-API-SECRET-KEY": self.config.secret,
            },
            params=params,
            timeout=self._timeout,
            opener=self._opener,
        )

    # --- positions ---
    def fetch_positions(self) -> List[Position]:
        payload = self._get("/v2/positions")
        rows = payload if isinstance(payload, list) else []

        positions: List[Position] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).strip().upper()
            quantity = to_float(row.get("qty"))
            entry = to_float(row.get("avg_entry_price"))
            if not symbol or quantity is None or entry is None:
                continue
            # Alpaca reports short size as a negative qty only in ``qty``; the
            # side field is the authoritative sign.
            if str(row.get("side", "long")).lower() == "short" and quantity > 0:
                quantity = -quantity

            multiplier = contract_multiplier(symbol)
            declared = to_float(row.get("multiplier"))
            if declared and declared >= 1:
                multiplier = int(declared)

            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry,
                    data_class=self.data_class,
                    mark_price=to_float(row.get("current_price")),
                    underlying=str(row.get("underlying_symbol") or underlying_of(symbol)),
                    contract_multiplier=multiplier,
                )
            )
        return positions

    # --- closed trades ---
    def fetch_trades(self, since: _dt.date, until: _dt.date) -> List[Trade]:
        """Rebuild round trips from the fill stream over the requested window.

        The window is widened backwards by ``lookback_days`` so a position
        opened before ``since`` and closed inside it still matches against its
        real entry instead of appearing as an orphan.
        """
        fills = self._fetch_fills(since - _dt.timedelta(days=LOOKBACK_DAYS), until)
        trades = match_fifo(fills, self.data_class)
        return [trade for trade in trades if since <= trade.trade_date <= until]

    def _fetch_fills(self, since: _dt.date, until: _dt.date) -> List[Fill]:
        fills: List[Fill] = []
        after = _dt.datetime.combine(since, _dt.time.min).isoformat()
        until_iso = _dt.datetime.combine(until, _dt.time.max).isoformat()

        for _page in range(MAX_PAGES):
            payload = self._get(
                "/v2/account/activities/FILL",
                {"after": after, "until": until_iso, "page_size": PAGE_SIZE, "direction": "asc"},
            )
            rows = payload if isinstance(payload, list) else []
            if not rows:
                break

            last_timestamp: Optional[str] = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                last_timestamp = str(row.get("transaction_time") or last_timestamp or "")
                fill = _fill_from_activity(row)
                if fill is not None:
                    fills.append(fill)

            if len(rows) < PAGE_SIZE or not last_timestamp:
                break
            # Alpaca paginates activities by timestamp cursor.
            after = last_timestamp
        return fills

    # --- account ---
    def _account(self) -> Dict[str, Any]:
        payload = self._get("/v2/account")
        return payload if isinstance(payload, dict) else {}

    def fetch_equity(self) -> Optional[float]:
        return to_float(self._account().get("equity"))

    def fetch_buying_power(self) -> Optional[float]:
        return to_float(self._account().get("buying_power"))


#: How far before the requested window to pull fills so entries are matchable.
LOOKBACK_DAYS = 30


def _fill_from_activity(row: Dict[str, Any]) -> Optional[Fill]:
    symbol = str(row.get("symbol", "")).strip().upper()
    side = str(row.get("side", "")).strip().lower()
    quantity = to_float(row.get("qty"))
    price = to_float(row.get("price"))
    filled_at = parse_timestamp(row.get("transaction_time"))
    if not symbol or not side or not quantity or price is None or filled_at is None:
        return None

    multiplier = contract_multiplier(symbol)
    declared = to_float(row.get("multiplier"))
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
