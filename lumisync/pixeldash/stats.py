"""Derived views over a snapshot: calendar layout, heat levels, streaks.

Everything here is a pure function of data the feeds returned. The one design
rule worth stating: a date with no trades is ``None``, never a zero-P&L day.
Those two states look identical in a naive aggregation and completely different
to someone reading a month at a glance.
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .models import DashboardSnapshot, DayStats

#: Heat buckets, as a fraction of the scale. Index 0 is flat; the sign of the
#: day's P&L picks the green or red ramp.
HEAT_STOPS: Tuple[float, ...] = (0.0, 0.12, 0.35, 0.65, 1.0)
MAX_HEAT = len(HEAT_STOPS) - 1


@dataclass(frozen=True)
class CalendarCell:
    """One square in the journal calendar."""

    date: _dt.date
    stats: Optional[DayStats]
    in_month: bool

    @property
    def traded(self) -> bool:
        return self.stats is not None and self.stats.trades > 0


@dataclass(frozen=True)
class Streak:
    """Consecutive green or red *trading* days, most recent first."""

    length: int = 0
    winning: bool = True

    @property
    def label(self) -> str:
        if not self.length:
            return "--"
        return f"W{self.length}" if self.winning else f"L{self.length}"


def heat_scale(days: Sequence[DayStats]) -> float:
    """Pick the P&L magnitude that maps to full colour saturation.

    The largest absolute day in the window sets the scale, so the calendar
    self-normalises to whatever size the account trades. Returns 0.0 when there
    is nothing to scale against, which callers read as "draw everything flat".
    """
    magnitudes = [abs(day.realized) for day in days if day.trades]
    return max(magnitudes) if magnitudes else 0.0


def heat_level(realized: float, scale: float) -> int:
    """Map a day's P&L onto a signed heat index in ``[-MAX_HEAT, MAX_HEAT]``."""
    if scale <= 0 or realized == 0:
        return 0
    fraction = min(1.0, abs(realized) / scale)
    level = 0
    for index, stop in enumerate(HEAT_STOPS):
        if fraction >= stop:
            level = index
    return level if realized > 0 else -level


def index_days(days: Sequence[DayStats]) -> Dict[_dt.date, DayStats]:
    return {day.date: day for day in days}


def month_grid(
    days: Sequence[DayStats],
    year: int,
    month: int,
    *,
    first_weekday: int = _cal.SUNDAY,
) -> List[List[CalendarCell]]:
    """Lay a month out as rows of seven cells, padded with adjacent days.

    Padding cells carry ``in_month=False`` so the renderer can dim them instead
    of dropping them, which keeps every week the same width.
    """
    lookup = index_days(days)
    weeks: List[List[CalendarCell]] = []
    for week in _cal.Calendar(firstweekday=first_weekday).monthdatescalendar(year, month):
        weeks.append(
            [
                CalendarCell(date=date, stats=lookup.get(date), in_month=date.month == month)
                for date in week
            ]
        )
    return weeks


def recent_grid(
    days: Sequence[DayStats],
    end: _dt.date,
    *,
    weeks: int = 7,
    first_weekday: int = _cal.SUNDAY,
) -> List[List[CalendarCell]]:
    """A rolling contribution-graph style grid ending on ``end``'s week.

    This is what fits a 52x32 panel: seven rows of days by N columns of weeks,
    the same shape as a commit heatmap, oriented weeks-across.
    """
    lookup = index_days(days)
    # Walk back to the first day of the week containing ``end``.
    offset = (end.weekday() - _weekday_index(first_weekday)) % 7
    week_start = end - _dt.timedelta(days=offset)
    first = week_start - _dt.timedelta(weeks=max(1, weeks) - 1)

    columns: List[List[CalendarCell]] = []
    for week in range(max(1, weeks)):
        column: List[CalendarCell] = []
        for day in range(7):
            date = first + _dt.timedelta(weeks=week, days=day)
            column.append(
                CalendarCell(date=date, stats=lookup.get(date), in_month=date <= end)
            )
        columns.append(column)
    return columns


def _weekday_index(first_weekday: int) -> int:
    """Convert a ``calendar`` constant to ``date.weekday()`` numbering."""
    # calendar uses MONDAY=0..SUNDAY=6 for firstweekday, matching date.weekday().
    return first_weekday % 7


def current_streak(days: Sequence[DayStats], *, before: Optional[_dt.date] = None) -> Streak:
    """Length of the current run of green or red days.

    Untraded days are skipped rather than breaking the streak — a weekend is not
    a losing day.
    """
    traded = [day for day in sorted(days, key=lambda item: item.date) if day.trades]
    if before is not None:
        traded = [day for day in traded if day.date <= before]
    if not traded:
        return Streak()

    winning = traded[-1].is_green
    length = 0
    for day in reversed(traded):
        if day.is_green != winning:
            break
        length += 1
    return Streak(length=length, winning=winning)


@dataclass(frozen=True)
class Summary:
    """The headline numbers for the daily scene."""

    realized_today: Optional[float]
    trades_today: int
    wins_today: int
    losses_today: int
    win_rate_today: Optional[float]
    open_positions: int
    open_unrealized: Optional[float]
    open_risk: Optional[float]
    streak: Streak
    window_realized: float
    window_trades: int
    rth_today: float
    eth_today: float

    @property
    def has_today(self) -> bool:
        return self.realized_today is not None


def summarize(snapshot: DashboardSnapshot) -> Summary:
    """Fold a snapshot into the numbers the daily card shows.

    ``realized_today`` is ``None`` when the account has not closed a trade
    today. That is rendered as "--", because "no trades yet" and "flat on the
    day" are different facts.
    """
    today = snapshot.today
    window_realized = sum(day.realized for day in snapshot.days)
    window_trades = sum(day.trades for day in snapshot.days)

    return Summary(
        realized_today=today.realized if today else None,
        trades_today=today.trades if today else 0,
        wins_today=today.wins if today else 0,
        losses_today=today.losses if today else 0,
        win_rate_today=today.win_rate if today else None,
        open_positions=len(snapshot.positions),
        open_unrealized=snapshot.open_unrealized,
        open_risk=snapshot.open_risk,
        streak=current_streak(snapshot.days),
        window_realized=window_realized,
        window_trades=window_trades,
        rth_today=today.rth_realized if today else 0.0,
        eth_today=today.eth_realized if today else 0.0,
    )
