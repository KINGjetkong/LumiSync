"""Feed parsing, credential resolution, and fail-closed behaviour."""

from __future__ import annotations

import datetime as _dt
import unittest
import urllib.error

from lumisync.pixeldash.config import (
    ALPACA_PAPER_HOST,
    TRADIER_LIVE_HOST,
    TRADIER_SANDBOX_HOST,
    load_dotenv,
    resolve_alpaca,
    resolve_tradier,
)
from lumisync.pixeldash.feeds.alpaca import AlpacaFeed
from lumisync.pixeldash.feeds.base import (
    FeedError,
    FeedUnconfigured,
    as_list,
    contract_multiplier,
    days_from_trades,
    parse_occ_symbol,
    parse_timestamp,
    to_float,
    underlying_of,
)
from lumisync.pixeldash.feeds.registry import build_feeds
from lumisync.pixeldash.feeds.roundtrip import Fill, match_fifo
from lumisync.pixeldash.feeds.tradier import TradierFeed, _mark_from_quote
from lumisync.pixeldash.models import DataClass, Session, session_for

from pixeldash_fixtures import RecordingOpener, config


class OccSymbolTests(unittest.TestCase):
    def test_parses_a_standard_option_symbol(self):
        parsed = parse_occ_symbol("SPY260803C00550000")
        self.assertIsNotNone(parsed)
        root, expiry, right, strike = parsed
        self.assertEqual(root, "SPY")
        self.assertEqual(expiry, _dt.date(2026, 8, 3))
        self.assertEqual(right, "C")
        self.assertEqual(strike, 550.0)

    def test_equity_symbols_are_not_options(self):
        self.assertIsNone(parse_occ_symbol("SPY"))
        self.assertEqual(contract_multiplier("SPY"), 1)
        self.assertEqual(contract_multiplier("SPY260803C00550000"), 100)

    def test_underlying_falls_back_to_the_symbol(self):
        self.assertEqual(underlying_of("QQQ260803P00470000"), "QQQ")
        self.assertEqual(underlying_of("aapl"), "AAPL")

    def test_malformed_symbols_do_not_raise(self):
        for value in ("", "1234567890123456", "SPY26AA03C00550000", None):
            self.assertIsNone(parse_occ_symbol(value or ""))


class ParsingHelperTests(unittest.TestCase):
    def test_as_list_normalizes_broker_shapes(self):
        self.assertEqual(as_list({"position": "null"}, "position"), [])
        self.assertEqual(as_list({"position": {"a": 1}}, "position"), [{"a": 1}])
        self.assertEqual(as_list({"position": [1, 2]}, "position"), [1, 2])
        self.assertEqual(as_list("null"), [])

    def test_to_float_distinguishes_missing_from_zero(self):
        self.assertIsNone(to_float(""))
        self.assertIsNone(to_float(None))
        self.assertIsNone(to_float("abc"))
        self.assertEqual(to_float("0"), 0.0)
        self.assertEqual(to_float("1.5"), 1.5)

    def test_parse_timestamp_handles_zulu_and_dates(self):
        self.assertIsNotNone(parse_timestamp("2026-08-03T14:30:00.000Z"))
        self.assertIsNotNone(parse_timestamp("2026-08-03"))
        self.assertIsNone(parse_timestamp("not a time"))


class CredentialTests(unittest.TestCase):
    def test_tradier_defaults_to_live(self):
        broker = resolve_tradier({"TRADIER_ACCESS_TOKEN": "t", "TRADIER_ACCOUNT_ID": "a"})
        self.assertTrue(broker.configured)
        self.assertIs(broker.data_class, DataClass.LIVE)
        self.assertEqual(broker.host, TRADIER_LIVE_HOST)

    def test_tradier_sandbox_is_tagged_paper(self):
        broker = resolve_tradier(
            {"TRADIER_ACCESS_TOKEN": "t", "TRADIER_ACCOUNT_ID": "a", "TRADIER_ENV": "sandbox"}
        )
        self.assertIs(broker.data_class, DataClass.PAPER)
        self.assertEqual(broker.host, TRADIER_SANDBOX_HOST)

    def test_partial_credentials_are_reported_not_ignored(self):
        broker = resolve_tradier({"TRADIER_ACCESS_TOKEN": "t"})
        self.assertTrue(broker.enabled)
        self.assertFalse(broker.configured)
        self.assertIn("TRADIER_ACCOUNT_ID", broker.missing)

    def test_alpaca_defaults_to_the_paper_endpoint(self):
        broker = resolve_alpaca({"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"})
        self.assertEqual(broker.host, ALPACA_PAPER_HOST)
        self.assertIs(broker.data_class, DataClass.PAPER)

    def test_alpaca_live_host_is_tagged_live(self):
        broker = resolve_alpaca(
            {
                "ALPACA_API_KEY_ID": "k",
                "ALPACA_API_SECRET_KEY": "s",
                "ALPACA_BASE_URL": "https://api.alpaca.markets",
            }
        )
        self.assertIs(broker.data_class, DataClass.LIVE)

    def test_dotenv_parsing(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# comment\nexport A=1\nB=\"two\"\nbad line\n")
            self.assertEqual(load_dotenv(path), {"A": "1", "B": "two"})

    def test_missing_dotenv_is_not_an_error(self):
        self.assertEqual(load_dotenv("/nonexistent/path/.env"), {})


class RegistryTests(unittest.TestCase):
    def test_no_credentials_yields_an_unconfigured_status(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            feeds, statuses = build_feeds(config(tmp))
        self.assertEqual(feeds, [])
        self.assertTrue(statuses)
        self.assertFalse(statuses[0].ok)

    def test_partial_credentials_surface_rather_than_vanish(self):
        import tempfile

        from lumisync.pixeldash.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config({"TRADIER_ACCESS_TOKEN": "t"}, output_dir=tmp)
            feeds, statuses = build_feeds(cfg)
        self.assertEqual(feeds, [])
        self.assertEqual(len(statuses), 1)
        self.assertIn("TRADIER_ACCOUNT_ID", statuses[0].detail)


TRADIER_POSITIONS = {
    "positions": {
        "position": [
            {
                "symbol": "SPY260803C00550000",
                "quantity": 10.0,
                "cost_basis": 1250.0,
                "date_acquired": "2026-08-03T13:45:00.000Z",
            }
        ]
    }
}
TRADIER_QUOTES = {
    "quotes": {"quote": {"symbol": "SPY260803C00550000", "bid": 1.50, "ask": 1.60, "last": 9.99}}
}
TRADIER_GAINLOSS = {
    "gainloss": {
        "closed_position": [
            {
                "symbol": "SPY260803C00550000",
                "close_date": "2026-08-03T15:10:00.000Z",
                "open_date": "2026-08-03T10:05:00.000Z",
                "gain_loss": 320.0,
                "quantity": 4.0,
                "cost": 500.0,
                "proceeds": 820.0,
            }
        ]
    }
}


def tradier_feed(opener) -> TradierFeed:
    broker = resolve_tradier({"TRADIER_ACCESS_TOKEN": "tok", "TRADIER_ACCOUNT_ID": "ACC123"})
    return TradierFeed(broker, opener=opener)


class TradierFeedTests(unittest.TestCase):
    def test_positions_use_the_nbbo_midpoint_not_last(self):
        opener = RecordingOpener({"/positions": TRADIER_POSITIONS, "/markets/quotes": TRADIER_QUOTES})
        positions = tradier_feed(opener).fetch_positions()

        self.assertEqual(len(positions), 1)
        held = positions[0]
        self.assertEqual(held.contract_multiplier, 100)
        self.assertAlmostEqual(held.entry_price, 1.25)
        self.assertAlmostEqual(held.mark_price, 1.55)
        self.assertAlmostEqual(held.unrealized, 300.0)

    def test_a_quote_failure_keeps_positions_but_drops_the_mark(self):
        opener = RecordingOpener(
            {
                "/positions": TRADIER_POSITIONS,
                "/markets/quotes": urllib.error.URLError("boom"),
            }
        )
        positions = tradier_feed(opener).fetch_positions()

        self.assertEqual(len(positions), 1)
        self.assertIsNone(positions[0].mark_price)
        # Crucially the unrealized number is refused rather than defaulted.
        self.assertIsNone(positions[0].unrealized)

    def test_mark_prefers_midpoint_then_last_then_close(self):
        self.assertAlmostEqual(_mark_from_quote({"bid": 1.0, "ask": 2.0, "last": 9.0}), 1.5)
        self.assertAlmostEqual(_mark_from_quote({"bid": 0, "ask": 0, "last": 9.0}), 9.0)
        self.assertAlmostEqual(_mark_from_quote({"close": 4.0}), 4.0)
        self.assertIsNone(_mark_from_quote({}))

    def test_closed_trades_come_from_the_broker_ledger(self):
        opener = RecordingOpener({"/gainloss": TRADIER_GAINLOSS})
        trades = tradier_feed(opener).fetch_trades(
            _dt.date(2026, 7, 1), _dt.date(2026, 8, 3)
        )

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 320.0)
        self.assertTrue(trades[0].is_win)
        self.assertAlmostEqual(trades[0].entry_price, 1.25)
        self.assertAlmostEqual(trades[0].exit_price, 2.05)

    def test_auth_failure_raises_unconfigured_not_empty_results(self):
        error = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        opener = RecordingOpener({"/positions": error})
        with self.assertRaises(FeedUnconfigured):
            tradier_feed(opener).fetch_positions()

    def test_server_error_raises_rather_than_returning_nothing(self):
        error = urllib.error.HTTPError("u", 503, "Service Unavailable", None, None)
        opener = RecordingOpener({"/positions": error})
        with self.assertRaises(FeedError):
            tradier_feed(opener).fetch_positions()

    def test_unconfigured_broker_refuses_construction(self):
        broker = resolve_tradier({"TRADIER_ACCESS_TOKEN": "tok"})
        with self.assertRaises(FeedUnconfigured):
            TradierFeed(broker)


ALPACA_POSITIONS = [
    {
        "symbol": "SPY260803C00550000",
        "qty": "10",
        "avg_entry_price": "1.25",
        "current_price": "1.55",
        "side": "long",
        "multiplier": "100",
    }
]
ALPACA_FILLS = [
    {
        "symbol": "AAPL",
        "side": "buy",
        "qty": "10",
        "price": "100",
        "transaction_time": "2026-08-03T14:00:00Z",
    },
    {
        "symbol": "AAPL",
        "side": "sell",
        "qty": "10",
        "price": "104",
        "transaction_time": "2026-08-03T15:00:00Z",
    },
]


def alpaca_feed(opener) -> AlpacaFeed:
    broker = resolve_alpaca({"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"})
    return AlpacaFeed(broker, opener=opener)


class AlpacaFeedTests(unittest.TestCase):
    def test_positions_carry_the_declared_multiplier(self):
        opener = RecordingOpener({"/v2/positions": ALPACA_POSITIONS})
        positions = alpaca_feed(opener).fetch_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].contract_multiplier, 100)
        self.assertAlmostEqual(positions[0].unrealized, 300.0)

    def test_trades_are_reconstructed_from_fills(self):
        opener = RecordingOpener({"/activities/FILL": ALPACA_FILLS})
        trades = alpaca_feed(opener).fetch_trades(_dt.date(2026, 8, 1), _dt.date(2026, 8, 3))

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 40.0)


class RoundTripTests(unittest.TestCase):
    @staticmethod
    def fill(side, quantity, price, minute, symbol="AAPL", multiplier=1):
        return Fill(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            filled_at=_dt.datetime(2026, 8, 3, 10, minute),
            multiplier=multiplier,
        )

    def test_simple_long_round_trip(self):
        trades = match_fifo(
            [self.fill("buy", 10, 100, 0), self.fill("sell", 10, 110, 5)], DataClass.PAPER
        )
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 100.0)

    def test_fifo_matches_the_oldest_lot_first(self):
        trades = match_fifo(
            [
                self.fill("buy", 10, 100, 0),
                self.fill("buy", 10, 120, 1),
                self.fill("sell", 10, 110, 5),
            ],
            DataClass.PAPER,
        )
        self.assertEqual(len(trades), 1)
        # Closed against the $100 lot, not the $120 one.
        self.assertAlmostEqual(trades[0].realized, 100.0)

    def test_short_round_trip_profits_when_price_falls(self):
        trades = match_fifo(
            [self.fill("sell", 5, 100, 0), self.fill("buy", 5, 90, 5)], DataClass.PAPER
        )
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 50.0)

    def test_partial_closes_split_into_separate_trades(self):
        trades = match_fifo(
            [
                self.fill("buy", 10, 100, 0),
                self.fill("sell", 4, 110, 3),
                self.fill("sell", 6, 105, 6),
            ],
            DataClass.PAPER,
        )
        self.assertEqual(len(trades), 2)
        self.assertAlmostEqual(sum(item.realized for item in trades), 70.0)

    def test_still_open_positions_produce_no_trade(self):
        self.assertEqual(match_fifo([self.fill("buy", 10, 100, 0)], DataClass.PAPER), [])

    def test_option_multiplier_scales_the_result(self):
        trades = match_fifo(
            [
                self.fill("buy", 1, 1.00, 0, symbol="SPY260803C00550000", multiplier=100),
                self.fill("sell", 1, 1.50, 5, symbol="SPY260803C00550000", multiplier=100),
            ],
            DataClass.LIVE,
        )
        self.assertAlmostEqual(trades[0].realized, 50.0)

    def test_out_of_order_fills_are_sorted_before_matching(self):
        trades = match_fifo(
            [self.fill("sell", 10, 110, 5), self.fill("buy", 10, 100, 0)], DataClass.PAPER
        )
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 100.0)


class DayAggregationTests(unittest.TestCase):
    def test_days_split_realized_by_session(self):
        from pixeldash_fixtures import trade

        days = days_from_trades(
            [trade(100.0, hour=11), trade(-40.0, hour=18)], DataClass.LIVE
        )
        self.assertEqual(len(days), 1)
        day = days[0]
        self.assertAlmostEqual(day.realized, 60.0)
        self.assertAlmostEqual(day.rth_realized, 100.0)
        self.assertAlmostEqual(day.eth_realized, -40.0)
        self.assertEqual((day.wins, day.losses, day.trades), (1, 1, 2))

    def test_untraded_dates_are_absent_rather_than_zero(self):
        from pixeldash_fixtures import trade

        days = days_from_trades([trade(10.0, days_ago=0), trade(10.0, days_ago=5)], DataClass.LIVE)
        self.assertEqual(len(days), 2)

    def test_session_classification(self):
        self.assertIs(session_for(_dt.datetime(2026, 8, 3, 10, 0)), Session.RTH)
        self.assertIs(session_for(_dt.datetime(2026, 8, 3, 18, 0)), Session.ETH)
        # Saturday is always extended hours, whatever the clock says.
        self.assertIs(session_for(_dt.datetime(2026, 8, 1, 11, 0)), Session.ETH)


if __name__ == "__main__":
    unittest.main()
