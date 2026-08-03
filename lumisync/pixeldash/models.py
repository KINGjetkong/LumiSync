"""Value types shared by feeds, stats, renderer and sinks.

Everything here is a frozen dataclass with an explicit ``data_class`` tag
(``live`` / ``paper`` / ``backtest``). The tag travels with the number all the
way to the panel so a figure on the wall can never be mistaken for a different
kind of figure — a paper fill and a live fill look different on screen.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Optional, Tuple

# US options regular trading hours, in exchange-local time. Anything outside
# this window is tagged ETH (extended hours) so the journal can split a day's
# P&L by session — the two behave differently enough that averaging them hides
# the signal.
RTH_OPEN = _dt.time(9, 30)
RTH_CLOSE = _dt.time(16, 0)
MARKET_TZ = "America/New_York"


class DataClass(str, Enum):
    """Where a number came from. Never inferred — always set by the feed."""

    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"

    @property
    def short(self) -> str:
        return {"live": "LIVE", "paper": "PAPR", "backtest": "BKTS"}[self.value]


class Session(str, Enum):
    """Which trading session a timestamp falls in."""

    RTH = "rth"
    ETH = "eth"


def market_tz() -> _dt.tzinfo:
    """Exchange timezone, falling back to UTC if tz data is unavailable."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(MARKET_TZ)
    except Exception:  # pragma: no cover - only on systems without tzdata
        return _dt.timezone.utc


def session_for(moment: _dt.datetime) -> Session:
    """Classify a timestamp as regular or extended hours.

    Naive timestamps are assumed to already be in exchange-local time; aware
    ones are converted. Weekends are ETH by definition.
    """
    local = moment.astimezone(market_tz()) if moment.tzinfo else moment
    if local.weekday() >= 5:
        return Session.ETH
    return Session.RTH if RTH_OPEN <= local.time() < RTH_CLOSE else Session.ETH


class FeedState(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class FeedStatus:
    """Health of one upstream feed.

    ``ERROR`` and ``UNCONFIGURED`` are rendered as visible failure states. They
    are never substituted with zeros — a blank panel and a flat-P&L panel mean
    very different things to the person reading it across the room.
    """

    name: str
    state: FeedState = FeedState.OK
    detail: str = ""
    checked_at: Optional[_dt.datetime] = None

    @property
    def ok(self) -> bool:
        return self.state is FeedState.OK

    @classmethod
    def error(cls, name: str, detail: str) -> "FeedStatus":
        return cls(name=name, state=FeedState.ERROR, detail=detail)

    @classmethod
    def unconfigured(cls, name: str, detail: str) -> "FeedStatus":
        return cls(name=name, state=FeedState.UNCONFIGURED, detail=detail)


@dataclass(frozen=True)
class Position:
    """One open position as the broker currently reports it."""

    symbol: str
    quantity: float
    entry_price: float
    data_class: DataClass
    opened_at: Optional[_dt.datetime] = None
    # ``None`` means the broker did not return a mark. It is NOT zero, and the
    # renderer shows it as "--" rather than inventing an unrealized number.
    mark_price: Optional[float] = None
    underlying: str = ""
    # 100 for standard US equity options, 1 for shares. Comes from the feed.
    contract_multiplier: int = 1

    @property
    def is_option(self) -> bool:
        return self.contract_multiplier != 1

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity * self.contract_multiplier

    @property
    def market_value(self) -> Optional[float]:
        if self.mark_price is None:
            return None
        return self.mark_price * self.quantity * self.contract_multiplier

    @property
    def unrealized(self) -> Optional[float]:
        value = self.market_value
        if value is None:
            return None
        return value - self.cost_basis

    @property
    def unrealized_pct(self) -> Optional[float]:
        basis = self.cost_basis
        if self.mark_price is None or basis == 0:
            return None
        return (self.market_value - basis) / abs(basis) * 100.0

    @property
    def session(self) -> Optional[Session]:
        return session_for(self.opened_at) if self.opened_at else None


@dataclass(frozen=True)
class Trade:
    """A closed round trip, as reported by the broker's gain/loss ledger."""

    symbol: str
    closed_at: _dt.datetime
    realized: float
    data_class: DataClass
    quantity: float = 0.0
    opened_at: Optional[_dt.datetime] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    underlying: str = ""

    @property
    def is_win(self) -> bool:
        return self.realized > 0

    @property
    def session(self) -> Session:
        return session_for(self.opened_at or self.closed_at)

    @property
    def trade_date(self) -> _dt.date:
        moment = self.closed_at
        local = moment.astimezone(market_tz()) if moment.tzinfo else moment
        return local.date()


@dataclass(frozen=True)
class DayStats:
    """Everything the calendar needs to colour one date."""

    date: _dt.date
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    rth_realized: float = 0.0
    eth_realized: float = 0.0
    data_class: DataClass = DataClass.LIVE
    note: str = ""

    @property
    def win_rate(self) -> Optional[float]:
        """Win rate as a percentage, or None when the day has no trades.

        Zero trades is not a zero win rate; the calendar renders it as an
        untraded day rather than a 0% day.
        """
        return (self.wins / self.trades * 100.0) if self.trades else None

    @property
    def is_green(self) -> bool:
        return self.realized > 0


@dataclass(frozen=True)
class AgentReport:
    """A status line from one of the workflow's agents.

    Agents are whatever pushes updates into the dash — the scoring engine, the
    exit state machine, a data-feed watchdog. Each renders as a pixel avatar
    with a state colour, so a glance at the panel says which part of the
    workflow just moved.
    """

    agent_id: str
    label: str
    state: str = "idle"          # idle | working | ok | warn | fail
    message: str = ""
    updated_at: Optional[_dt.datetime] = None


@dataclass(frozen=True)
class DashboardSnapshot:
    """One immutable read of the whole workflow.

    This is the only thing the renderer sees. If a field is missing it is
    ``None`` — never a filler value — so the scene composer can decide between
    "no data yet" and "the number really is zero".
    """

    captured_at: _dt.datetime
    data_class: DataClass
    account_label: str = ""
    positions: Tuple[Position, ...] = ()
    trades: Tuple[Trade, ...] = ()
    days: Tuple[DayStats, ...] = ()
    agents: Tuple[AgentReport, ...] = ()
    feeds: Tuple[FeedStatus, ...] = ()
    equity: Optional[float] = None
    buying_power: Optional[float] = None

    # --- derived reads -------------------------------------------------
    @property
    def healthy(self) -> bool:
        return all(status.ok for status in self.feeds) and bool(self.feeds)

    @property
    def failed_feeds(self) -> List[FeedStatus]:
        return [status for status in self.feeds if not status.ok]

    @property
    def today(self) -> Optional[DayStats]:
        local = self.captured_at
        local = local.astimezone(market_tz()) if local.tzinfo else local
        return self.day(local.date())

    def day(self, when: _dt.date) -> Optional[DayStats]:
        for entry in self.days:
            if entry.date == when:
                return entry
        return None

    @property
    def open_risk(self) -> Optional[float]:
        """Total cost basis currently at risk, or None with no positions."""
        if not self.positions:
            return None
        return sum(position.cost_basis for position in self.positions)

    @property
    def open_unrealized(self) -> Optional[float]:
        """Sum of unrealized P&L, or None if any position lacks a mark.

        Partial sums are deliberately refused: a total that silently omits an
        unmarked leg is a wrong number, not an approximate one.
        """
        if not self.positions:
            return None
        values = [position.unrealized for position in self.positions]
        if any(value is None for value in values):
            return None
        return sum(values)

    def with_agents(self, agents: Tuple[AgentReport, ...]) -> "DashboardSnapshot":
        return replace(self, agents=agents)


@dataclass
class JournalNote:
    """A free-text note the trader attaches to a date."""

    date: _dt.date
    text: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderTarget:
    """Geometry of a physical or virtual panel.

    Defaults match the Govee Gaming Pixel Light H6631 (52x32).
    """

    cols: int = 52
    rows: int = 32
    name: str = "H6631"

    @property
    def size(self) -> Tuple[int, int]:
        return (self.cols, self.rows)

    @property
    def pixels(self) -> int:
        return self.cols * self.rows


#: Geometries Pixel Dash knows how to compose scenes for. Add a size here and
#: every scene lays itself out against it — the composers read the target's
#: layout metrics, they do not hard-code 52x32.
#:
#: The ``screen`` sizes exist because a monitor is not a panel. Hardware pixel
#: count is fixed on an LED matrix, but a hover overlay or a ghost display can
#: draw as many pixels as you like — so those targets use a denser grid, which
#: gives the layout room for the 5x7 face and makes the text markedly easier to
#: read. ``screen`` and ``screen-xl`` are exact 2x and 3x multiples of the
#: H6631 grid, so a layout checked on one is proportionally identical on the
#: others.
KNOWN_TARGETS: Dict[str, RenderTarget] = {
    "H6631": RenderTarget(52, 32, "H6631"),
    "screen": RenderTarget(104, 64, "screen"),
    "screen-xl": RenderTarget(156, 96, "screen-xl"),
    "64x32": RenderTarget(64, 32, "64x32"),
    "32x32": RenderTarget(32, 32, "32x32"),
    "16x32": RenderTarget(32, 16, "16x32"),
    "16x16": RenderTarget(16, 16, "16x16"),
}

#: Default geometry for on-screen surfaces (hover, ghost). Panels keep their
#: own hardware size; only the screen sinks get the denser grid.
DEFAULT_SCREEN_TARGET = "screen"
