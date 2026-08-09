"""Shared builders for the Pixel Dash tests.

These construct :class:`DashboardSnapshot` values directly. They are test
fixtures for the *renderer*, not a data source — nothing in this module is
importable from ``lumisync.pixeldash``, and no product code path can reach it.
The rule the package enforces (real broker data or an explicit error state) is
about what ships, and these values never leave the test suite.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
from typing import Any, Dict, List, Optional, Sequence

from lumisync.pixeldash.config import PixelDashConfig, load_config
from lumisync.pixeldash.feeds.base import days_from_trades
from lumisync.pixeldash.models import (
    AgentReport,
    DashboardSnapshot,
    DataClass,
    FeedStatus,
    Position,
    Trade,
    market_tz,
)

MOMENT = _dt.datetime(2026, 8, 3, 14, 30, tzinfo=market_tz())


def config(tmpdir: str, **overrides) -> PixelDashConfig:
    """A config with no broker credentials and an isolated output directory."""
    return load_config({}, output_dir=tmpdir, **overrides)


def position(
    symbol: str = "SPY260803C00550000",
    quantity: float = 10,
    entry: float = 1.25,
    mark: Optional[float] = 1.55,
    data_class: DataClass = DataClass.LIVE,
) -> Position:
    return Position(
        symbol=symbol,
        quantity=quantity,
        entry_price=entry,
        data_class=data_class,
        opened_at=MOMENT,
        mark_price=mark,
        underlying=symbol[:3],
        contract_multiplier=100,
    )


def trade(
    realized: float,
    *,
    days_ago: int = 0,
    symbol: str = "SPY260803C00550000",
    hour: int = 11,
    data_class: DataClass = DataClass.LIVE,
) -> Trade:
    closed = (MOMENT - _dt.timedelta(days=days_ago)).replace(hour=hour, minute=15)
    return Trade(
        symbol=symbol,
        closed_at=closed,
        realized=realized,
        data_class=data_class,
        quantity=5,
        opened_at=closed - _dt.timedelta(minutes=20),
        underlying=symbol[:3],
    )


def trade_today(realized: float, *, hour: int = 11, symbol: str = "SPY260803C00550000") -> Trade:
    """A trade closed on the real current date.

    Most fixtures hang off the fixed :data:`MOMENT` so renders stay
    reproducible. Anything that goes through the live service is different: the
    collector stamps snapshots with the real clock, so a trade pinned to a fixed
    date stops being "today" the moment the calendar moves past it — which is
    exactly how this suite broke once already.
    """
    closed = _dt.datetime.now(tz=market_tz()).replace(
        hour=hour, minute=15, second=0, microsecond=0
    )
    return Trade(
        symbol=symbol,
        closed_at=closed,
        realized=realized,
        data_class=DataClass.LIVE,
        quantity=5,
        opened_at=closed - _dt.timedelta(minutes=20),
        underlying=symbol[:3],
    )


def snapshot(
    *,
    positions: Sequence[Position] = (),
    trades: Sequence[Trade] = (),
    agents: Sequence[AgentReport] = (),
    feeds: Sequence[FeedStatus] = (FeedStatus("tradier"),),
    data_class: DataClass = DataClass.LIVE,
    captured_at: _dt.datetime = MOMENT,
) -> DashboardSnapshot:
    trade_list = list(trades)
    return DashboardSnapshot(
        captured_at=captured_at,
        data_class=data_class,
        account_label="TRADIER 1234",
        positions=tuple(positions),
        trades=tuple(trade_list),
        days=tuple(days_from_trades(trade_list, data_class)),
        agents=tuple(agents),
        feeds=tuple(feeds),
    )


def busy_snapshot() -> DashboardSnapshot:
    """A snapshot that exercises every scene: positions, history and agents."""
    return snapshot(
        positions=[
            position(),
            position(symbol="QQQ260803P00470000", quantity=5, entry=2.10, mark=1.80),
        ],
        trades=[
            trade(240.0, days_ago=0),
            trade(-90.0, days_ago=0, hour=18),
            trade(410.0, days_ago=1),
            trade(-150.0, days_ago=3),
            trade(75.0, days_ago=8),
        ],
        agents=[
            AgentReport("tradier", "TRADIER", "ok", "2 open / 5 closed", MOMENT),
            AgentReport("exits", "EXITS", "warn", "armed floor near", MOMENT),
        ],
    )


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class RecordingOpener:
    """Serves canned JSON by URL substring and records what was requested."""

    def __init__(self, routes: Dict[str, Any]) -> None:
        self.routes = routes
        self.requests: List[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(json.dumps(payload).encode())
        raise AssertionError(f"no fake route matched {url}")
