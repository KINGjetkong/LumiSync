"""Tradier brokerage feed — positions, closed round trips, balances.

Tradier is the account-of-record in this stack, so it is the default source for
everything the dash shows about money. Two details matter for correctness:

* Tradier's ``/positions`` payload has no price on it. Marks come from a
  separate ``/markets/quotes`` call, and an option's mark is the NBBO midpoint,
  not ``last`` — a stale print on an illiquid 0DTE strike would misstate open
  P&L by more than the position is worth.
* ``/gainloss`` is the realized ledger. It is authoritative for the calendar;
  nothing is reconstructed locally when the broker already computed it.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from ..config import BrokerConfig
from ..models import DataClass, Position, Trade
from .base import (
    FeedError,
    FeedUnconfigured,
    TradeFeed,
    as_list,
    contract_multiplier,
    get_json,
    parse_timestamp,
    to_float,
    underlying_of,
)

#: Tradier caps ``/gainloss`` at 100 rows per page.
PAGE_SIZE = 100
MAX_PAGES = 20


class TradierFeed(TradeFeed):
    name = "tradier"

    def __init__(self, config: BrokerConfig, *, opener=None, timeout: float = 8.0) -> None:
        if not config.configured:
            missing = ", ".join(config.missing) or "credentials"
            raise FeedUnconfigured(f"Tradier is missing {missing}")
        self.config = config
        self._opener = opener
        self._timeout = timeout

    @property
    def data_class(self) -> DataClass:
        return self.config.data_class

    def account_label(self) -> str:
        account = self.config.account
        return f"TRADIER {account[-4:]}" if len(account) > 4 else "TRADIER"

    # --- transport ---
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return get_json(
            f"{self.config.host}{path}",
            headers={"Authorization": f"Bearer {self.config.token}"},
            params=params,
            timeout=self._timeout,
            opener=self._opener,
        )

    def _account_path(self, suffix: str) -> str:
        return f"/v1/accounts/{self.config.account}{suffix}"

    # --- positions ---
    def fetch_positions(self) -> List[Position]:
        payload = self._get(self._account_path("/positions"))
        rows = as_list(payload.get("positions") if isinstance(payload, dict) else None, "position")

        raw: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).strip().upper()
            quantity = to_float(row.get("quantity"))
            cost_basis = to_float(row.get("cost_basis"))
            if not symbol or not quantity:
                continue
            raw.append({"symbol": symbol, "quantity": quantity, "cost_basis": cost_basis, "row": row})

        marks = self._fetch_marks([entry["symbol"] for entry in raw])

        positions: List[Position] = []
        for entry in raw:
            symbol = entry["symbol"]
            quantity = entry["quantity"]
            multiplier = contract_multiplier(symbol)
            cost_basis = entry["cost_basis"]
            # Tradier reports total cost basis; per-unit entry is what the panel
            # shows next to the mark, so divide by notional size.
            notional = quantity * multiplier
            entry_price = (cost_basis / notional) if cost_basis is not None and notional else 0.0
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    data_class=self.data_class,
                    opened_at=parse_timestamp(entry["row"].get("date_acquired")),
                    mark_price=marks.get(symbol),
                    underlying=underlying_of(symbol),
                    contract_multiplier=multiplier,
                )
            )
        return positions

    def _fetch_marks(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        """Look up NBBO midpoints for the given symbols.

        A quote failure is not fatal: positions still render, with ``--`` where
        the mark would be. Losing the whole positions card because a quote
        endpoint hiccuped would be a worse trade-off than showing the gap.
        """
        if not symbols:
            return {}
        try:
            payload = self._get("/v1/markets/quotes", {"symbols": ",".join(sorted(set(symbols)))})
        except FeedError:
            return {}

        quotes = as_list(payload.get("quotes") if isinstance(payload, dict) else None, "quote")
        marks: Dict[str, Optional[float]] = {}
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            symbol = str(quote.get("symbol", "")).strip().upper()
            if symbol:
                marks[symbol] = _mark_from_quote(quote)
        return marks

    # --- closed trades ---
    def fetch_trades(self, since: _dt.date, until: _dt.date) -> List[Trade]:
        trades: List[Trade] = []
        for page in range(1, MAX_PAGES + 1):
            payload = self._get(
                self._account_path("/gainloss"),
                {
                    "page": page,
                    "limit": PAGE_SIZE,
                    "sortBy": "closeDate",
                    "sort": "desc",
                    "start": since.isoformat(),
                    "end": until.isoformat(),
                },
            )
            rows = as_list(
                payload.get("gainloss") if isinstance(payload, dict) else None, "closed_position"
            )
            if not rows:
                break

            for row in rows:
                trade = _trade_from_gainloss(row, self.data_class)
                if trade is not None:
                    trades.append(trade)
            if len(rows) < PAGE_SIZE:
                break
        return trades

    # --- balances ---
    def _balances(self) -> Dict[str, Any]:
        payload = self._get(self._account_path("/balances"))
        balances = payload.get("balances") if isinstance(payload, dict) else None
        return balances if isinstance(balances, dict) else {}

    def fetch_equity(self) -> Optional[float]:
        return to_float(self._balances().get("total_equity"))

    def fetch_buying_power(self) -> Optional[float]:
        balances = self._balances()
        for key in ("option_buying_power", "stock_buying_power"):
            container = balances.get("margin") or balances.get("cash") or balances
            if isinstance(container, dict) and key in container:
                value = to_float(container.get(key))
                if value is not None:
                    return value
        return to_float(balances.get("total_cash"))


def _mark_from_quote(quote: Dict[str, Any]) -> Optional[float]:
    """NBBO midpoint, falling back to last, then to the previous close.

    Returns ``None`` when the quote carries no usable price at all rather than
    defaulting to zero.
    """
    bid = to_float(quote.get("bid"))
    ask = to_float(quote.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    for key in ("last", "close", "prevclose"):
        value = to_float(quote.get(key))
        if value is not None and value > 0:
            return value
    return None


def _trade_from_gainloss(row: Any, data_class: DataClass) -> Optional[Trade]:
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol", "")).strip().upper()
    closed_at = parse_timestamp(row.get("close_date"))
    realized = to_float(row.get("gain_loss"))
    if not symbol or closed_at is None or realized is None:
        return None

    quantity = to_float(row.get("quantity")) or 0.0
    multiplier = contract_multiplier(symbol)
    notional = quantity * multiplier
    cost = to_float(row.get("cost"))
    proceeds = to_float(row.get("proceeds"))

    return Trade(
        symbol=symbol,
        closed_at=closed_at,
        realized=realized,
        data_class=data_class,
        quantity=quantity,
        opened_at=parse_timestamp(row.get("open_date")),
        entry_price=(cost / notional) if cost is not None and notional else None,
        exit_price=(proceeds / notional) if proceeds is not None and notional else None,
        underlying=underlying_of(symbol),
    )
