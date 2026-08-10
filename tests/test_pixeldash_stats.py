"""Calendar layout, heat scaling, streaks and the daily summary."""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
import os
import tempfile
import unittest

from lumisync.pixeldash.journal import Journal
from lumisync.pixeldash.models import DataClass, DayStats
from lumisync.pixeldash.stats import (
    MAX_HEAT,
    Streak,
    current_streak,
    heat_level,
    heat_scale,
    month_grid,
    recent_grid,
    summarize,
)

from pixeldash_fixtures import busy_snapshot, position, snapshot


def day(date: _dt.date, realized: float, trades: int = 1, wins: int = 1, losses: int = 0) -> DayStats:
    return DayStats(
        date=date, realized=realized, trades=trades, wins=wins, losses=losses,
        data_class=DataClass.LIVE,
    )


class HeatTests(unittest.TestCase):
    def test_the_biggest_day_sets_the_scale(self):
        days = [day(_dt.date(2026, 8, 1), 100.0), day(_dt.date(2026, 8, 2), -900.0)]
        self.assertEqual(heat_scale(days), 900.0)

    def test_untraded_days_do_not_set_the_scale(self):
        days = [DayStats(date=_dt.date(2026, 8, 1), realized=9999.0, trades=0)]
        self.assertEqual(heat_scale(days), 0.0)

    def test_heat_level_is_signed_and_bounded(self):
        self.assertEqual(heat_level(1000.0, 1000.0), MAX_HEAT)
        self.assertEqual(heat_level(-1000.0, 1000.0), -MAX_HEAT)
        self.assertEqual(heat_level(0.0, 1000.0), 0)
        self.assertEqual(heat_level(500.0, 0.0), 0)

    def test_a_bigger_day_never_gets_a_cooler_level(self):
        levels = [heat_level(value, 1000.0) for value in (10, 200, 500, 800, 1000)]
        self.assertEqual(levels, sorted(levels))


class CalendarTests(unittest.TestCase):
    def test_month_grid_is_always_seven_wide(self):
        weeks = month_grid([day(_dt.date(2026, 8, 3), 100.0)], 2026, 8)
        self.assertTrue(weeks)
        for week in weeks:
            self.assertEqual(len(week), 7)

    def test_padding_days_are_marked_out_of_month(self):
        weeks = month_grid([], 2026, 8)
        flat = [cell for week in weeks for cell in week]
        self.assertTrue(any(not cell.in_month for cell in flat))

    def test_recent_grid_ends_on_the_requested_week(self):
        end = _dt.date(2026, 8, 3)
        columns = recent_grid([day(end, 100.0)], end, weeks=4)
        self.assertEqual(len(columns), 4)
        for column in columns:
            self.assertEqual(len(column), 7)

        dates = [cell.date for column in columns for cell in column]
        self.assertIn(end, dates)

    def test_recent_grid_attaches_stats_to_the_right_date(self):
        end = _dt.date(2026, 8, 3)
        columns = recent_grid([day(end, 250.0)], end, weeks=2)
        matched = [
            cell for column in columns for cell in column if cell.date == end
        ]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0].traded)
        self.assertAlmostEqual(matched[0].stats.realized, 250.0)

    def test_untraded_dates_have_no_stats(self):
        end = _dt.date(2026, 8, 3)
        columns = recent_grid([], end, weeks=2)
        self.assertTrue(all(not cell.traded for column in columns for cell in column))

    def test_grid_respects_a_monday_start(self):
        end = _dt.date(2026, 8, 5)  # a Wednesday
        columns = recent_grid([], end, weeks=1, first_weekday=_cal.MONDAY)
        self.assertEqual(columns[0][0].date.weekday(), 0)


class StreakTests(unittest.TestCase):
    def test_consecutive_green_days_accumulate(self):
        days = [
            day(_dt.date(2026, 8, 1), 100.0),
            day(_dt.date(2026, 8, 2), 200.0),
            day(_dt.date(2026, 8, 3), 50.0),
        ]
        streak = current_streak(days)
        self.assertEqual((streak.length, streak.winning), (3, True))
        self.assertEqual(streak.label, "W3")

    def test_a_red_day_breaks_the_run(self):
        days = [
            day(_dt.date(2026, 8, 1), 100.0),
            day(_dt.date(2026, 8, 2), -200.0),
            day(_dt.date(2026, 8, 3), -50.0),
        ]
        streak = current_streak(days)
        self.assertEqual((streak.length, streak.winning), (2, False))
        self.assertEqual(streak.label, "L2")

    def test_untraded_days_do_not_break_a_streak(self):
        days = [
            day(_dt.date(2026, 8, 1), 100.0),
            DayStats(date=_dt.date(2026, 8, 2), trades=0),
            day(_dt.date(2026, 8, 3), 100.0),
        ]
        self.assertEqual(current_streak(days).length, 2)

    def test_no_history_yields_an_empty_streak(self):
        self.assertEqual(current_streak([]), Streak())
        self.assertEqual(current_streak([]).label, "--")


class SummaryTests(unittest.TestCase):
    def test_no_trades_today_reports_none_not_zero(self):
        summary = summarize(snapshot())
        self.assertIsNone(summary.realized_today)
        self.assertIsNone(summary.win_rate_today)
        self.assertFalse(summary.has_today)

    def test_today_aggregates_wins_losses_and_sessions(self):
        summary = summarize(busy_snapshot())
        self.assertTrue(summary.has_today)
        self.assertAlmostEqual(summary.realized_today, 150.0)
        self.assertEqual((summary.wins_today, summary.losses_today), (1, 1))
        self.assertAlmostEqual(summary.rth_today, 240.0)
        self.assertAlmostEqual(summary.eth_today, -90.0)

    def test_open_unrealized_is_refused_when_a_leg_has_no_mark(self):
        partial = snapshot(positions=[position(), position(symbol="QQQ260803P00470000", mark=None)])
        self.assertIsNone(summarize(partial).open_unrealized)

    def test_open_unrealized_sums_when_every_leg_is_marked(self):
        both = snapshot(positions=[position(), position(symbol="QQQ260803P00470000", mark=1.55)])
        self.assertIsNotNone(summarize(both).open_unrealized)

    def test_window_totals_cover_the_whole_history(self):
        summary = summarize(busy_snapshot())
        self.assertEqual(summary.window_trades, 5)
        self.assertAlmostEqual(summary.window_realized, 485.0)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_notes_persist_across_instances(self):
        journal = Journal(self.path)
        journal.set(_dt.date(2026, 8, 3), "chased a gap, paid for it", ["revenge"])

        reloaded = Journal(self.path)
        note = reloaded.get(_dt.date(2026, 8, 3))
        self.assertIsNotNone(note)
        self.assertEqual(note.text, "chased a gap, paid for it")
        self.assertEqual(note.tags, ["revenge"])

    def test_clearing_a_note_removes_it(self):
        journal = Journal(self.path)
        journal.set(_dt.date(2026, 8, 3), "temporary")
        journal.set(_dt.date(2026, 8, 3), "   ")
        self.assertIsNone(Journal(self.path).get(_dt.date(2026, 8, 3)))

    def test_a_corrupt_file_yields_an_empty_journal(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(Journal(self.path).get(_dt.date(2026, 8, 3)))

    def test_annotate_attaches_text_without_touching_numbers(self):
        journal = Journal(self.path)
        target = _dt.date(2026, 8, 3)
        journal.set(target, "held the runner")

        annotated = journal.annotate([day(target, 500.0)])
        self.assertEqual(annotated[0].note, "held the runner")
        self.assertAlmostEqual(annotated[0].realized, 500.0)

    def test_missing_file_is_a_normal_empty_state(self):
        self.assertIsNone(Journal(os.path.join(self.tmp.name, "nope.json")).get(_dt.date.today()))


if __name__ == "__main__":
    unittest.main()
