"""FIFO round-trip reconstruction from a raw fill stream.

Some brokers (Alpaca among them) expose fills but no realized gain/loss ledger.
Matching them into round trips locally is the only way to get a truthful
per-trade record — and it must be FIFO to match how the broker itself reports
cost basis, or the calendar and the tax record disagree.

This is pure arithmetic over data the broker actually sent. It never fabricates
a fill, and unmatched opening fills simply stay open rather than being closed at
an assumed price.
"""

from __future__ import annotations

import datetime as _dt
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List

from ..models import DataClass, Trade


@dataclass(frozen=True)
class Fill:
    """One execution as the broker reported it."""

    symbol: str
    side: str            # "buy" or "sell"
    quantity: float      # always positive
    price: float
    filled_at: _dt.datetime
    multiplier: int = 1

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side.lower().startswith("b") else -self.quantity


@dataclass
class _Lot:
    quantity: float      # signed: positive long, negative short
    price: float
    opened_at: _dt.datetime


def match_fifo(fills: Iterable[Fill], data_class: DataClass) -> List[Trade]:
    """Fold fills into closed round trips, oldest lot closed first.

    Fills are sorted by time first, so an out-of-order page from the broker
    cannot corrupt the matching. Positions still open at the end of the stream
    produce no trade — they are open positions, and the positions feed owns
    them.
    """
    from .base import underlying_of

    books: Dict[str, Deque[_Lot]] = {}
    trades: List[Trade] = []

    for fill in sorted(fills, key=lambda item: item.filled_at):
        book = books.setdefault(fill.symbol, deque())
        remaining = fill.signed_quantity

        while remaining and book and _opposes(book[0].quantity, remaining):
            lot = book[0]
            matched = min(abs(lot.quantity), abs(remaining))
            was_long = lot.quantity > 0
            direction = 1.0 if was_long else -1.0
            realized = (fill.price - lot.price) * direction * matched * fill.multiplier

            trades.append(
                Trade(
                    symbol=fill.symbol,
                    closed_at=fill.filled_at,
                    realized=realized,
                    data_class=data_class,
                    quantity=matched,
                    opened_at=lot.opened_at,
                    entry_price=lot.price,
                    exit_price=fill.price,
                    underlying=underlying_of(fill.symbol),
                )
            )

            lot.quantity -= matched if was_long else -matched
            remaining += matched if was_long else -matched
            if abs(lot.quantity) < 1e-9:
                book.popleft()

        if remaining:
            book.append(_Lot(quantity=remaining, price=fill.price, opened_at=fill.filled_at))

    trades.sort(key=lambda trade: trade.closed_at)
    return trades


def _opposes(lot_quantity: float, fill_quantity: float) -> bool:
    """True when a fill reduces an existing lot instead of adding to it."""
    return (lot_quantity > 0) != (fill_quantity > 0)
