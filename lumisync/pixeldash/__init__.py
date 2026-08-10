"""Pixel Dash — a trading dashboard that renders itself as pixel art.

The pipeline is deliberately one-way and side-effect free until the very end:

    broker feed  ->  DashboardSnapshot  ->  ScenePlan  ->  frames  ->  GIF  ->  sinks

Two rules shape every module in here:

* **Real data or an error state.** Feeds never invent numbers. A missing
  credential, a dead endpoint or a stale quote produces a
  :class:`~lumisync.pixeldash.models.FeedStatus` in the ``error`` state, and the
  renderer draws that error on the panel. There is no demo mode and no
  placeholder P&L anywhere in this package.
* **Deterministic rendering.** The same snapshot always renders to the same
  bytes. Every source of variation is seeded from a digest of the snapshot
  itself (see :mod:`lumisync.pixeldash.render.pipeline`), so a frame can be
  reproduced from a journal entry weeks later.
"""

from .models import (
    DashboardSnapshot,
    DataClass,
    DayStats,
    FeedStatus,
    Position,
    Trade,
)

__all__ = [
    "DashboardSnapshot",
    "DataClass",
    "DayStats",
    "FeedStatus",
    "Position",
    "Trade",
]
