"""Turn the difference between two snapshots into notifications.

The dash polls; it does not receive pushes. So "something happened" has to be
derived by comparing consecutive snapshots. Two properties make that safe:

* **Idempotent.** Every event carries a stable ``key``. Re-polling the same
  state produces the same keys, and :class:`EventLog` drops repeats — a trade
  cannot celebrate itself twice because the poller ran again.
* **Ordered.** Events come back in a fixed order for a given pair of snapshots,
  so the notification queue is reproducible.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import DashboardSnapshot, FeedStatus, Position, Trade


class EventKind(str, Enum):
    WIN = "win"                      # a closed trade finished green
    LOSS = "loss"                    # a closed trade finished red — "mission failed"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    AGENT_UPDATE = "agent_update"
    FEED_DOWN = "feed_down"
    FEED_RECOVERED = "feed_recovered"

    @property
    def severity(self) -> str:
        return {
            EventKind.WIN: "good",
            EventKind.LOSS: "bad",
            EventKind.POSITION_OPENED: "info",
            EventKind.POSITION_CLOSED: "info",
            EventKind.AGENT_UPDATE: "info",
            EventKind.FEED_DOWN: "bad",
            EventKind.FEED_RECOVERED: "good",
        }[self]


@dataclass(frozen=True)
class DashEvent:
    """One thing worth interrupting the scene rotation for."""

    kind: EventKind
    key: str
    title: str
    detail: str = ""
    amount: Optional[float] = None
    at: Optional[_dt.datetime] = None

    @property
    def severity(self) -> str:
        return self.kind.severity


def _trade_key(trade: Trade) -> str:
    """Stable identity for a closed trade.

    Brokers do not expose a round-trip id, so identity is composed from the
    fields that cannot collide for the same account: symbol, close time, size
    and realized amount.
    """
    return (
        f"trade:{trade.symbol}:{trade.closed_at.isoformat()}"
        f":{trade.quantity:g}:{trade.realized:.4f}"
    )


def _position_key(position: Position) -> str:
    return f"position:{position.symbol}:{position.quantity:g}"


def diff(
    previous: Optional[DashboardSnapshot], current: DashboardSnapshot
) -> List[DashEvent]:
    """Events implied by moving from ``previous`` to ``current``.

    With no previous snapshot the result is empty: the first poll of a session
    would otherwise fire a notification for every trade already on the books.
    """
    if previous is None:
        return []

    # State transitions (feeds, agents) are stamped with this snapshot's capture
    # time. ``diff`` only emits them on an actual change, so the stamp is what
    # lets a feed that goes down, recovers, and goes down again fire all three
    # times instead of being swallowed by :class:`EventLog`'s dedupe.
    stamp = current.captured_at.isoformat()

    events: List[DashEvent] = []
    events.extend(_trade_events(previous.trades, current.trades))
    events.extend(_position_events(previous.positions, current.positions))
    events.extend(_feed_events(previous.feeds, current.feeds, stamp))
    events.extend(_agent_events(previous, current, stamp))
    return events


def _trade_events(before: Sequence[Trade], after: Sequence[Trade]) -> List[DashEvent]:
    known = {_trade_key(trade) for trade in before}
    events: List[DashEvent] = []
    for trade in sorted(after, key=lambda item: item.closed_at):
        key = _trade_key(trade)
        if key in known:
            continue
        won = trade.is_win
        events.append(
            DashEvent(
                kind=EventKind.WIN if won else EventKind.LOSS,
                key=key,
                title="ANOTHER WIN" if won else "MISSION FAILED",
                detail=trade.underlying or trade.symbol,
                amount=trade.realized,
                at=trade.closed_at,
            )
        )
    return events


def _position_events(
    before: Sequence[Position], after: Sequence[Position]
) -> List[DashEvent]:
    before_by_symbol = {position.symbol: position for position in before}
    after_by_symbol = {position.symbol: position for position in after}
    events: List[DashEvent] = []

    for symbol in sorted(after_by_symbol.keys() - before_by_symbol.keys()):
        position = after_by_symbol[symbol]
        events.append(
            DashEvent(
                kind=EventKind.POSITION_OPENED,
                key=_position_key(position),
                title="POSITION OPEN",
                detail=f"{position.underlying or symbol} x{position.quantity:g}",
                amount=position.cost_basis,
                at=position.opened_at,
            )
        )

    for symbol in sorted(before_by_symbol.keys() - after_by_symbol.keys()):
        position = before_by_symbol[symbol]
        events.append(
            DashEvent(
                kind=EventKind.POSITION_CLOSED,
                key=f"closed:{_position_key(position)}",
                title="POSITION CLOSED",
                detail=position.underlying or symbol,
            )
        )
    return events


def _feed_events(
    before: Sequence[FeedStatus], after: Sequence[FeedStatus], stamp: str
) -> List[DashEvent]:
    was_ok = {status.name: status.ok for status in before}
    events: List[DashEvent] = []
    for status in sorted(after, key=lambda item: item.name):
        previous_ok = was_ok.get(status.name)
        if previous_ok is None or previous_ok == status.ok:
            continue
        if status.ok:
            events.append(
                DashEvent(
                    kind=EventKind.FEED_RECOVERED,
                    key=f"feed:{status.name}:up:{stamp}",
                    title="FEED BACK",
                    detail=status.name.upper(),
                )
            )
        else:
            events.append(
                DashEvent(
                    kind=EventKind.FEED_DOWN,
                    key=f"feed:{status.name}:down:{stamp}",
                    title="FEED DOWN",
                    detail=f"{status.name.upper()} {status.detail}".strip()[:32],
                )
            )
    return events


def _agent_events(
    previous: DashboardSnapshot, current: DashboardSnapshot, stamp: str
) -> List[DashEvent]:
    """Fire when an agent changes *state* — the "something moved in the
    workflow" signal the agent avatars announce.

    Only the state is compared, never the message. A feed agent's message
    carries live counts ("2 open / 31 closed") that change on almost every
    poll; firing on those would interrupt the scene rotation with a full-panel
    burst every thirty seconds and train the operator to ignore the one signal
    that is supposed to be worth looking up for. The current message still
    rides along on the event and on the agents card.
    """
    before = {agent.agent_id: agent for agent in previous.agents}
    events: List[DashEvent] = []
    for agent in sorted(current.agents, key=lambda item: item.agent_id):
        prior = before.get(agent.agent_id)
        if prior is not None and prior.state == agent.state:
            continue
        if prior is None and agent.state in ("idle", "ok"):
            # A newly-registered healthy agent is not news.
            continue
        events.append(
            DashEvent(
                kind=EventKind.AGENT_UPDATE,
                key=f"agent:{agent.agent_id}:{agent.state}:{stamp}",
                title=agent.label.upper()[:12],
                detail=agent.message[:32],
                at=agent.updated_at,
            )
        )
    return events


class EventLog:
    """Bounded, de-duplicating queue of events awaiting display."""

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = max(1, capacity)
        self._seen: Dict[str, None] = {}
        self._pending: List[DashEvent] = []

    def extend(self, events: Iterable[DashEvent]) -> List[DashEvent]:
        """Queue events that have not been seen before; return the new ones."""
        fresh: List[DashEvent] = []
        for event in events:
            if event.key in self._seen:
                continue
            self._seen[event.key] = None
            fresh.append(event)
        self._pending.extend(fresh)
        self._trim()
        return fresh

    def pop(self) -> Optional[DashEvent]:
        return self._pending.pop(0) if self._pending else None

    def drain(self) -> Tuple[DashEvent, ...]:
        pending, self._pending = tuple(self._pending), []
        return pending

    @property
    def pending(self) -> Tuple[DashEvent, ...]:
        return tuple(self._pending)

    def _trim(self) -> None:
        overflow = len(self._pending) - self.capacity
        if overflow > 0:
            del self._pending[:overflow]
        # Keep the dedupe set from growing without bound over a long session.
        limit = self.capacity * 16
        if len(self._seen) > limit:
            for key in list(self._seen)[: len(self._seen) - limit]:
                self._seen.pop(key, None)
