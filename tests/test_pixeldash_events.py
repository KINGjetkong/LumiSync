"""Notification derivation: what fires, once, and in what order."""

from __future__ import annotations

import unittest

from lumisync.pixeldash.events import DashEvent, EventKind, EventLog, diff
from lumisync.pixeldash.models import AgentReport, FeedStatus

from pixeldash_fixtures import MOMENT, position, snapshot, trade


class DiffTests(unittest.TestCase):
    def test_the_first_snapshot_fires_nothing(self):
        # Otherwise starting the app would celebrate every trade already booked.
        self.assertEqual(diff(None, snapshot(trades=[trade(100.0)])), [])

    def test_a_new_winning_trade_fires_a_win(self):
        before = snapshot()
        after = snapshot(trades=[trade(240.0)])
        events = diff(before, after)

        self.assertEqual([event.kind for event in events], [EventKind.WIN])
        self.assertEqual(events[0].title, "ANOTHER WIN")
        self.assertEqual(events[0].severity, "good")
        self.assertAlmostEqual(events[0].amount, 240.0)

    def test_a_new_losing_trade_fires_mission_failed(self):
        events = diff(snapshot(), snapshot(trades=[trade(-90.0)]))
        self.assertEqual(events[0].kind, EventKind.LOSS)
        self.assertEqual(events[0].title, "MISSION FAILED")
        self.assertEqual(events[0].severity, "bad")

    def test_an_unchanged_snapshot_fires_nothing(self):
        snap = snapshot(trades=[trade(100.0)], positions=[position()])
        self.assertEqual(diff(snap, snap), [])

    def test_opening_a_position_fires_once(self):
        events = diff(snapshot(), snapshot(positions=[position()]))
        self.assertEqual([event.kind for event in events], [EventKind.POSITION_OPENED])
        self.assertIn("SPY", events[0].detail)

    def test_closing_a_position_is_reported(self):
        events = diff(snapshot(positions=[position()]), snapshot())
        self.assertEqual([event.kind for event in events], [EventKind.POSITION_CLOSED])

    def test_a_feed_going_down_fires(self):
        before = snapshot(feeds=[FeedStatus("tradier")])
        after = snapshot(feeds=[FeedStatus.error("tradier", "HTTP 503")])
        events = diff(before, after)

        self.assertEqual([event.kind for event in events], [EventKind.FEED_DOWN])
        self.assertIn("TRADIER", events[0].detail)

    def test_a_feed_recovering_fires(self):
        before = snapshot(feeds=[FeedStatus.error("tradier", "HTTP 503")])
        after = snapshot(feeds=[FeedStatus("tradier")])
        self.assertEqual([event.kind for event in diff(before, after)], [EventKind.FEED_RECOVERED])

    def test_a_steadily_down_feed_does_not_re_fire(self):
        down = snapshot(feeds=[FeedStatus.error("tradier", "HTTP 503")])
        self.assertEqual(diff(down, down), [])

    def test_an_agent_message_changing_does_not_fire(self):
        # Feed agents report live counts. Firing on those would burst the panel
        # on every poll and make the signal worthless.
        before = snapshot(agents=[AgentReport("tradier", "TRADIER", "ok", "2 open / 30 closed", MOMENT)])
        after = snapshot(agents=[AgentReport("tradier", "TRADIER", "ok", "2 open / 31 closed", MOMENT)])
        self.assertEqual(diff(before, after), [])

    def test_a_feed_can_go_down_twice(self):
        import datetime as _dt

        up = snapshot(feeds=[FeedStatus("tradier")])
        down = snapshot(feeds=[FeedStatus.error("tradier", "HTTP 503")])
        later_down = snapshot(
            feeds=[FeedStatus.error("tradier", "HTTP 503")],
            captured_at=MOMENT + _dt.timedelta(minutes=5),
        )

        log = EventLog()
        self.assertEqual(len(log.extend(diff(up, down))), 1)
        self.assertEqual(len(log.extend(diff(down, up))), 1)
        # A second outage must not be swallowed by the dedupe of the first.
        self.assertEqual(len(log.extend(diff(up, later_down))), 1)

    def test_an_agent_changing_state_fires(self):
        before = snapshot(agents=[AgentReport("exits", "EXITS", "ok", "idle", MOMENT)])
        after = snapshot(agents=[AgentReport("exits", "EXITS", "warn", "floor armed", MOMENT)])
        events = diff(before, after)

        self.assertEqual([event.kind for event in events], [EventKind.AGENT_UPDATE])
        self.assertEqual(events[0].title, "EXITS")

    def test_a_new_healthy_agent_is_not_news(self):
        after = snapshot(agents=[AgentReport("exits", "EXITS", "ok", "all good", MOMENT)])
        self.assertEqual(diff(snapshot(), after), [])

    def test_a_new_failing_agent_is_news(self):
        after = snapshot(agents=[AgentReport("exits", "EXITS", "fail", "no quotes", MOMENT)])
        self.assertEqual([event.kind for event in diff(snapshot(), after)], [EventKind.AGENT_UPDATE])

    def test_ordering_is_stable_for_the_same_pair(self):
        before = snapshot()
        after = snapshot(
            trades=[trade(10.0), trade(-10.0, hour=12)],
            positions=[position()],
            agents=[AgentReport("exits", "EXITS", "fail", "down", MOMENT)],
        )
        self.assertEqual([event.key for event in diff(before, after)],
                         [event.key for event in diff(before, after)])


class EventLogTests(unittest.TestCase):
    @staticmethod
    def event(key: str) -> DashEvent:
        return DashEvent(EventKind.WIN, key, "ANOTHER WIN", "SPY", 10.0, MOMENT)

    def test_repeated_keys_are_dropped(self):
        log = EventLog()
        self.assertEqual(len(log.extend([self.event("a"), self.event("b")])), 2)
        self.assertEqual(log.extend([self.event("a")]), [])

    def test_drain_empties_the_queue(self):
        log = EventLog()
        log.extend([self.event("a")])
        self.assertEqual(len(log.drain()), 1)
        self.assertEqual(log.pending, ())

    def test_pop_returns_events_in_order(self):
        log = EventLog()
        log.extend([self.event("a"), self.event("b")])
        self.assertEqual(log.pop().key, "a")
        self.assertEqual(log.pop().key, "b")
        self.assertIsNone(log.pop())

    def test_the_queue_is_bounded(self):
        log = EventLog(capacity=3)
        log.extend([self.event(str(index)) for index in range(10)])
        self.assertEqual(len(log.pending), 3)

    def test_dedupe_survives_the_queue_being_drained(self):
        log = EventLog()
        log.extend([self.event("a")])
        log.drain()
        self.assertEqual(log.extend([self.event("a")]), [])


if __name__ == "__main__":
    unittest.main()
