"""The poll loop: collection, fault isolation, and the CLI surface."""

from __future__ import annotations

import datetime as _dt
import tempfile
import threading
import unittest

from lumisync.pixeldash import cli
from lumisync.pixeldash.collector import collect
from lumisync.pixeldash.events import EventKind
from lumisync.pixeldash.feeds.base import FeedError, TradeFeed
from lumisync.pixeldash.models import DataClass, market_tz
from lumisync.pixeldash.service import PixelDashService
from lumisync.pixeldash.sinks.base import Sink, SinkReport

from pixeldash_fixtures import MOMENT, config, position, trade, trade_today


class StubFeed(TradeFeed):
    """A feed whose responses the test dictates, including failure."""

    def __init__(self, name, *, positions=(), trades=(), data_class=DataClass.PAPER, error=None):
        self.name = name
        self._positions = list(positions)
        self._trades = list(trades)
        self._data_class = data_class
        self._error = error
        self.calls = 0

    @property
    def data_class(self):
        return self._data_class

    def fetch_positions(self):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._positions)

    def fetch_trades(self, since, until):
        if self._error:
            raise self._error
        return list(self._trades)

    def fetch_equity(self):
        return 10_000.0

    def account_label(self):
        return self.name.upper()


class RecordingSink(Sink):
    name = "recording"

    def __init__(self):
        self.published = []

    def publish(self, result):
        self.published.append(result)
        return SinkReport(sink=self.name, ok=True)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_healthy_feed_populates_the_snapshot(self):
        feed = StubFeed("tradier", positions=[position()], trades=[trade(120.0)])
        snapshot = collect(self.config, feeds=[feed], statuses=[], now=MOMENT)

        self.assertTrue(snapshot.healthy)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(len(snapshot.days), 1)
        self.assertEqual(snapshot.equity, 10_000.0)

    def test_a_failing_feed_becomes_an_error_status_not_an_empty_account(self):
        feed = StubFeed("tradier", error=FeedError("HTTP 503"))
        snapshot = collect(self.config, feeds=[feed], statuses=[], now=MOMENT)

        self.assertFalse(snapshot.healthy)
        self.assertEqual(len(snapshot.failed_feeds), 1)
        self.assertIn("503", snapshot.failed_feeds[0].detail)
        self.assertEqual(snapshot.positions, ())

    def test_one_broken_feed_does_not_hide_a_healthy_one(self):
        good = StubFeed("alpaca", positions=[position()])
        bad = StubFeed("tradier", error=FeedError("down"))
        snapshot = collect(self.config, feeds=[good, bad], statuses=[], now=MOMENT)

        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(len(snapshot.failed_feeds), 1)

    def test_a_failing_live_feed_does_not_stamp_the_panel_live(self):
        live = StubFeed("tradier", data_class=DataClass.LIVE, error=FeedError("down"))
        paper = StubFeed("alpaca", data_class=DataClass.PAPER, positions=[position()])
        snapshot = collect(self.config, feeds=[live, paper], statuses=[], now=MOMENT)

        self.assertIs(snapshot.data_class, DataClass.PAPER)

    def test_a_healthy_live_feed_wins_the_label(self):
        live = StubFeed("tradier", data_class=DataClass.LIVE, positions=[position()])
        paper = StubFeed("alpaca", data_class=DataClass.PAPER)
        snapshot = collect(self.config, feeds=[live, paper], statuses=[], now=MOMENT)

        self.assertIs(snapshot.data_class, DataClass.LIVE)

    def test_a_driver_bug_is_contained(self):
        class Exploding(StubFeed):
            def fetch_positions(self):
                raise ValueError("driver bug")

        snapshot = collect(self.config, feeds=[Exploding("tradier")], statuses=[], now=MOMENT)
        self.assertFalse(snapshot.healthy)
        self.assertIn("ValueError", snapshot.failed_feeds[0].detail)

    def test_positions_are_ordered_by_risk(self):
        small = position(symbol="QQQ260803P00470000", quantity=1)
        large = position(symbol="SPY260803C00550000", quantity=20)
        snapshot = collect(
            self.config, feeds=[StubFeed("t", positions=[small, large])], statuses=[], now=MOMENT
        )
        self.assertEqual(snapshot.positions[0].symbol, large.symbol)

    def test_agent_reports_mirror_feed_health(self):
        snapshot = collect(
            self.config,
            feeds=[StubFeed("tradier", error=FeedError("down"))],
            statuses=[],
            now=MOMENT,
        )
        self.assertEqual(snapshot.agents[0].state, "fail")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, feeds, sinks=()):
        return PixelDashService(self.config, sinks=list(sinks), feeds=feeds)

    def test_a_refresh_renders_and_publishes(self):
        sink = RecordingSink()
        service = self.service([StubFeed("tradier", trades=[trade(100.0)])], [sink])
        try:
            tick = service.refresh()
            self.assertTrue(tick.ok)
            self.assertEqual(len(sink.published), 1)
            self.assertGreater(tick.result.frame_count, 0)
        finally:
            service.close()

    def test_publish_can_be_suppressed(self):
        sink = RecordingSink()
        service = self.service([StubFeed("tradier")], [sink])
        try:
            service.refresh(publish=False)
            self.assertEqual(sink.published, [])
        finally:
            service.close()

    def test_the_first_tick_fires_no_events(self):
        service = self.service([StubFeed("tradier", trades=[trade(100.0)])])
        try:
            self.assertEqual(service.refresh(publish=False).events, ())
        finally:
            service.close()

    def test_a_new_trade_on_the_second_tick_fires_a_win(self):
        feed = StubFeed("tradier")
        service = self.service([feed])
        try:
            service.refresh(publish=False)
            feed._trades = [trade(250.0)]
            tick = service.refresh(publish=False)
            self.assertEqual([event.kind for event in tick.events], [EventKind.WIN])
        finally:
            service.close()

    def test_the_same_trade_does_not_fire_twice(self):
        feed = StubFeed("tradier")
        service = self.service([feed])
        try:
            service.refresh(publish=False)
            feed._trades = [trade(250.0)]
            service.refresh(publish=False)
            self.assertEqual(service.refresh(publish=False).events, ())
        finally:
            service.close()

    def test_a_down_feed_still_produces_a_render(self):
        service = self.service([StubFeed("tradier", error=FeedError("HTTP 503"))])
        try:
            tick = service.refresh(publish=False)
            self.assertTrue(tick.ok)
            self.assertIn("error", tick.result.scene_ids)
        finally:
            service.close()

    def test_journal_notes_reach_the_snapshot(self):
        # Dated to the real clock, not the fixed fixture moment: the collector
        # stamps snapshots with the current date, so a trade pinned to a fixed
        # day stops being "today" as soon as the calendar moves past it.
        service = self.service([StubFeed("tradier", trades=[trade_today(100.0)])])
        try:
            today = service.config and _dt.datetime.now(tz=market_tz()).date()
            service.journal.set(today, "held the runner")
            tick = service.refresh(publish=False)

            self.assertIsNotNone(tick.snapshot.today, "the trade should land on today")
            self.assertEqual(tick.snapshot.today.note, "held the runner")
        finally:
            service.close()

    def test_run_forever_stops_after_the_tick_budget(self):
        feed = StubFeed("tradier")
        service = self.service([feed])
        service.config.refresh_seconds = 0.01
        seen = []
        try:
            service.run_forever(on_tick=seen.append, stop=threading.Event(), max_ticks=3)
            self.assertEqual(len(seen), 3)
        finally:
            service.close()

    def test_a_raising_observer_does_not_kill_the_loop(self):
        service = self.service([StubFeed("tradier")])
        service.config.refresh_seconds = 0.01

        def observer(_tick):
            raise RuntimeError("observer bug")

        try:
            service.run_forever(on_tick=observer, stop=threading.Event(), max_ticks=2)
        finally:
            service.close()

    def test_feeds_are_built_once_and_reused(self):
        feed = StubFeed("tradier")
        service = self.service([feed])
        try:
            service.refresh(publish=False)
            service.refresh(publish=False)
            self.assertEqual(feed.calls, 2)  # two polls, one construction
            self.assertIs(service.ensure_feeds()[0][0], feed)
        finally:
            service.close()


class CliTests(unittest.TestCase):
    def test_check_reports_failure_when_nothing_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["check", "--output", tmp]), 1)

    def test_once_renders_the_setup_card_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            import os

            self.assertEqual(cli.main(["once", "--output", tmp]), 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "dashboard.gif")))

    def test_target_override_changes_the_panel_geometry(self):
        args = cli.build_parser().parse_args(["once", "--target", "32x32"])
        self.assertEqual(cli.config_from_args(args).target.cols, 32)

    def test_overrides_reach_the_config(self):
        args = cli.build_parser().parse_args(
            ["run", "--interval", "5", "--frame-ms", "80", "--days", "30", "--no-planner"]
        )
        built = cli.config_from_args(args)
        self.assertEqual(built.refresh_seconds, 5)
        self.assertEqual(built.frame_ms, 80)
        self.assertEqual(built.calendar_days, 30)
        self.assertFalse(built.planner_enabled)


if __name__ == "__main__":
    unittest.main()
