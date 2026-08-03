"""Assemble one :class:`DashboardSnapshot` from every configured feed.

Collection is deliberately fault-isolated per feed: one broker timing out
degrades that broker's card to an error state and leaves the rest of the panel
truthful. What it must never do is let a failure look like an empty account, so
every failure lands in ``snapshot.feeds`` where the renderer will draw it.
"""

from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Sequence, Tuple

from .config import PixelDashConfig
from .feeds.base import FeedError, TradeFeed, days_from_trades
from .feeds.registry import build_feeds
from .models import (
    AgentReport,
    DashboardSnapshot,
    DataClass,
    DayStats,
    FeedStatus,
    Position,
    Trade,
    market_tz,
)


def now_market() -> _dt.datetime:
    """Current time in exchange-local terms — the clock the dash reasons in."""
    return _dt.datetime.now(tz=market_tz())


def collect(
    config: PixelDashConfig,
    *,
    feeds: Optional[Sequence[TradeFeed]] = None,
    statuses: Optional[Sequence[FeedStatus]] = None,
    now: Optional[_dt.datetime] = None,
) -> DashboardSnapshot:
    """Poll every feed once and fold the results into a snapshot."""
    moment = now or now_market()
    if feeds is None:
        built_feeds, built_statuses = build_feeds(config)
        feeds = built_feeds
        statuses = list(statuses or []) + list(built_statuses)
    statuses = list(statuses or [])

    until = moment.date()
    since = until - _dt.timedelta(days=max(1, config.calendar_days))

    positions: List[Position] = []
    trades: List[Trade] = []
    agents: List[AgentReport] = []
    equity: Optional[float] = None
    buying_power: Optional[float] = None
    labels: List[str] = []

    for feed in feeds:
        feed_positions, feed_trades, status, agent = _poll(feed, since, until)
        statuses.append(status)
        agents.append(agent)
        if not status.ok:
            continue
        positions.extend(feed_positions)
        trades.extend(feed_trades)
        labels.append(feed.account_label())
        equity = _accumulate(equity, _safe(feed.fetch_equity))
        buying_power = _accumulate(buying_power, _safe(feed.fetch_buying_power))

    data_class = _data_class_for(feeds, statuses)
    days: Tuple[DayStats, ...] = tuple(days_from_trades(trades, data_class))

    return DashboardSnapshot(
        captured_at=moment,
        data_class=data_class,
        account_label=" + ".join(labels),
        positions=tuple(sorted(positions, key=_position_sort_key)),
        trades=tuple(sorted(trades, key=lambda trade: trade.closed_at)),
        days=days,
        agents=tuple(agents),
        feeds=tuple(statuses),
        equity=equity,
        buying_power=buying_power,
    )


def _poll(
    feed: TradeFeed, since: _dt.date, until: _dt.date
) -> Tuple[List[Position], List[Trade], FeedStatus, AgentReport]:
    """Pull one feed, converting any failure into a reportable status."""
    try:
        positions = feed.fetch_positions()
        trades = feed.fetch_trades(since, until)
    except FeedError as exc:
        return [], [], FeedStatus.error(feed.name, str(exc)), feed.agent_report("fail", str(exc)[:40])
    except Exception as exc:  # a driver bug must not take the whole dash down
        detail = f"{type(exc).__name__}: {exc}"
        return [], [], FeedStatus.error(feed.name, detail), feed.agent_report("fail", detail[:40])

    summary = f"{len(positions)} open / {len(trades)} closed"
    return positions, trades, feed.status_ok(), feed.agent_report("ok", summary)


def _safe(call):
    """Call an optional balance accessor, returning None on any failure."""
    try:
        return call()
    except Exception:
        return None


def _accumulate(total: Optional[float], value: Optional[float]) -> Optional[float]:
    """Sum values across feeds, keeping None when nothing reported a number."""
    if value is None:
        return total
    return value if total is None else total + value


def _position_sort_key(position: Position):
    """Biggest risk first — that is what the eye should land on."""
    return (-abs(position.cost_basis), position.symbol)


def _data_class_for(
    feeds: Sequence[TradeFeed], statuses: Sequence[FeedStatus]
) -> DataClass:
    """Label the snapshot with the strictest class among *healthy* feeds.

    A failed live feed must not stamp the panel LIVE while every number on it
    came from paper.
    """
    healthy = {status.name for status in statuses if status.ok}
    classes = [feed.data_class for feed in feeds if feed.name in healthy]
    if DataClass.LIVE in classes:
        return DataClass.LIVE
    return DataClass.PAPER
