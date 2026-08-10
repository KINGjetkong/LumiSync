"""The loop that ties everything together.

One tick is: poll the brokers, merge journal notes, diff against the previous
snapshot for notifications, render, and fan out to the sinks. The service holds
no Qt dependency so it runs identically under the GUI and under the CLI.

Failure policy throughout: a tick that cannot collect still renders — the
snapshot it renders carries the feed errors, and the error card is the output.
The loop only stops when it is told to.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .collector import collect, now_market
from .config import PixelDashConfig
from .events import DashEvent, EventLog, diff
from .feeds.base import TradeFeed
from .feeds.registry import build_feeds
from .journal import Journal, default_journal_path
from .models import KNOWN_TARGETS, DashboardSnapshot, FeedStatus
from .render import planner
from .render.pipeline import RenderResult, render
from .render.planner import PlannerHook
from .sinks.base import Sink, SinkReport, close_all, group_by_target, publish_all


@dataclass
class ServiceTick:
    """The result of one refresh.

    ``result`` is the panel-geometry render — what the preview and the panel
    sink use. ``renders`` holds every geometry produced this tick, keyed by
    target name, because the screen surfaces render on a denser grid than the
    panel and both are real outputs of the same snapshot.
    """

    snapshot: DashboardSnapshot
    result: Optional[RenderResult] = None
    events: Tuple[DashEvent, ...] = ()
    reports: List[SinkReport] = field(default_factory=list)
    renders: Dict[str, RenderResult] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.result is not None

    @property
    def degraded_sinks(self) -> List[SinkReport]:
        return [report for report in self.reports if report.ok and report.degraded]

    def summary(self) -> str:
        if self.error:
            return f"tick failed: {self.error}"
        pieces = [
            f"{self.snapshot.data_class.short}",
            f"{len(self.snapshot.positions)} open",
            f"{len(self.snapshot.days)} days",
        ]
        if self.events:
            pieces.append(f"{len(self.events)} events")
        if self.result is not None:
            pieces.append(f"{self.result.frame_count}f {self.result.digest[:8]}")
        return " · ".join(pieces)


TickCallback = Callable[[ServiceTick], None]


class PixelDashService:
    """Owns the feeds, the journal, the event log and the sinks."""

    def __init__(
        self,
        config: PixelDashConfig,
        *,
        sinks: Optional[Sequence[Sink]] = None,
        feeds: Optional[Sequence[TradeFeed]] = None,
        journal: Optional[Journal] = None,
        planner_hook: Optional[PlannerHook] = None,
    ) -> None:
        self.config = config
        self.sinks: List[Sink] = list(sinks or [])
        self.planner_hook = planner_hook
        self.events = EventLog()

        self._feeds: List[TradeFeed] = []
        self._feed_statuses: List[FeedStatus] = []
        self._feeds_built = feeds is not None
        if feeds is not None:
            self._feeds = list(feeds)

        self.journal = journal or Journal(default_journal_path(config.output_dir))
        self.previous: Optional[DashboardSnapshot] = None
        self.last_tick: Optional[ServiceTick] = None
        self._stop = threading.Event()

    # --- feeds ---
    def ensure_feeds(self) -> Tuple[List[TradeFeed], List[FeedStatus]]:
        """Build feeds once, then reuse them.

        Rebuilding on every tick would re-resolve credentials constantly and
        make a transient auth failure look permanent.
        """
        if not self._feeds_built:
            self._feeds, self._feed_statuses = build_feeds(self.config)
            self._feeds_built = True
        return self._feeds, self._feed_statuses

    def reload_feeds(self) -> None:
        """Force a rebuild — call after credentials change."""
        self._feeds_built = False
        self._feeds = []
        self._feed_statuses = []

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)
            try:
                sink.close()
            except Exception:
                pass

    # --- one tick ---
    def refresh(self, *, publish: bool = True) -> ServiceTick:
        """Poll, render, and (optionally) publish once."""
        feeds, statuses = self.ensure_feeds()

        try:
            snapshot = collect(self.config, feeds=feeds, statuses=statuses, now=now_market())
        except Exception as exc:
            # collect() already isolates per-feed failures, so reaching here
            # means something structural broke. Report it; do not fabricate a
            # snapshot to render.
            tick = ServiceTick(
                snapshot=_empty_snapshot(str(exc)),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.last_tick = tick
            return tick

        from dataclasses import replace

        snapshot = replace(snapshot, days=self.journal.annotate(snapshot.days))

        fresh = self.events.extend(diff(self.previous, snapshot))
        self.previous = snapshot

        try:
            renders = self.render_all(snapshot, fresh)
        except Exception as exc:
            tick = ServiceTick(
                snapshot=snapshot,
                events=tuple(fresh),
                error=f"render failed: {type(exc).__name__}: {exc}",
            )
            self.last_tick = tick
            return tick

        reports: List[SinkReport] = []
        if publish and self.sinks:
            for target_name, sinks in group_by_target(self.sinks, self.config.target.name).items():
                result = renders.get(target_name)
                if result is not None:
                    reports.extend(publish_all(sinks, result))

        tick = ServiceTick(
            snapshot=snapshot,
            result=renders.get(self.config.target.name),
            events=tuple(fresh),
            reports=reports,
            renders=renders,
        )
        self.last_tick = tick
        return tick

    def render_all(
        self, snapshot: DashboardSnapshot, events: Sequence[DashEvent]
    ) -> Dict[str, RenderResult]:
        """Render this snapshot once per geometry any sink asked for.

        The panel geometry is always produced, even with no sinks attached, so
        the GUI preview and ``--once`` always have something to show. The plan
        is resolved once and shared: it is a property of the situation, not of
        the geometry, and re-resolving it per target would consult the planner
        cache several times for the same answer.
        """
        wanted = {self.config.target.name}
        for sink in self.sinks:
            wanted.add(getattr(sink, "target", "") or self.config.target.name)

        shared_plan = planner.plan(snapshot, self.config, hook=self.planner_hook)

        renders: Dict[str, RenderResult] = {}
        for name in sorted(wanted):
            target = KNOWN_TARGETS.get(name, self.config.target)
            renders[name] = render(
                snapshot,
                self.config,
                events=events,
                plan=shared_plan,
                target=target,
            )
        return renders

    # --- loop ---
    def run_forever(
        self,
        *,
        on_tick: Optional[TickCallback] = None,
        stop: Optional[threading.Event] = None,
        max_ticks: int = 0,
    ) -> None:
        """Refresh on the configured interval until stopped.

        ``max_ticks`` bounds the loop, which is how the tests drive it.
        """
        stop = stop or self._stop
        stop.clear()
        ticks = 0

        while not stop.is_set():
            tick = self.refresh()
            if on_tick is not None:
                try:
                    on_tick(tick)
                except Exception:
                    # A misbehaving observer must not kill the loop.
                    pass

            ticks += 1
            if max_ticks and ticks >= max_ticks:
                return
            stop.wait(max(1.0, float(self.config.refresh_seconds)))

    @property
    def stop_event(self) -> threading.Event:
        """The event :meth:`run_forever` watches, for callers driving the loop."""
        return self._stop

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()
        close_all(self.sinks)
        self.sinks = []


def _empty_snapshot(detail: str) -> DashboardSnapshot:
    """A snapshot that carries nothing but the failure that produced it."""
    from .models import DataClass

    return DashboardSnapshot(
        captured_at=_dt.datetime.now(),
        data_class=DataClass.PAPER,
        feeds=(FeedStatus.error("collector", detail),),
    )
