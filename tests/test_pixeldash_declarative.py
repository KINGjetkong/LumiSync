"""Declarative API specs, plus the standalone Govee scene bridge."""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error

from lumisync.pixeldash.events import DashEvent, EventKind
from lumisync.pixeldash.feeds.base import FeedUnconfigured
from lumisync.pixeldash.feeds.declarative import (
    DeclarativeFeed,
    Endpoint,
    SpecError,
    dig,
    parse_spec,
    records_from,
)
from lumisync.pixeldash.feeds.registry import build_declarative_feeds
from lumisync.pixeldash.feeds.specs import (
    EXAMPLE_SPEC,
    describe,
    ensure_feeds_dir,
    load_specs,
)
from lumisync.pixeldash.models import DataClass
from lumisync.pixeldash.render.pipeline import render

from pixeldash_fixtures import MOMENT, RecordingOpener, busy_snapshot, config, snapshot

# The Govee bridge deliberately lives outside the package: LumiSync's runtime is
# cloud-free by design and a test enforces it, so anything talking to a vendor
# cloud ships as a standalone tool.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
from govee_scene_bridge import (  # noqa: E402
    BridgeConfig,
    GoveeBridge,
    rgb_to_int,
    state_from_manifest,
)

VALID_SPEC = {
    "name": "myprop",
    "base_url": "https://api.example.com/",
    "data_class": "live",
    "account_label": "PROP",
    "auth": {"type": "bearer", "token_env": "MYPROP_TOKEN"},
    "positions": {
        "path": "/v1/positions",
        "records": "data.rows",
        "fields": {
            "symbol": "ticker",
            "quantity": "qty",
            "entry_price": "avg",
            "mark_price": "last",
        },
    },
    "trades": {
        "path": "/v1/trades",
        "records": "data.rows",
        "params": {"start": "{since}", "end": "{until}"},
        "fields": {"symbol": "ticker", "realized": "pnl", "closed_at": "closed"},
    },
}

POSITIONS_RESPONSE = {
    "data": {"rows": [{"ticker": "SPY260803C00550000", "qty": 10, "avg": 1.25, "last": 1.55}]}
}
TRADES_RESPONSE = {
    "data": {"rows": [{"ticker": "SPY", "pnl": 240.0, "closed": "2026-08-03T15:00:00Z"}]}
}


class PathTests(unittest.TestCase):
    def test_dig_follows_dotted_paths(self):
        payload = {"data": {"rows": [{"a": 1}]}}
        self.assertEqual(dig(payload, "data.rows.0.a"), 1)

    def test_dig_returns_none_for_a_dead_path(self):
        self.assertIsNone(dig({"a": 1}, "a.b.c"))
        self.assertIsNone(dig({"a": 1}, "missing"))
        self.assertIsNone(dig(None, "a"))

    def test_dig_with_an_empty_path_is_identity(self):
        self.assertEqual(dig({"a": 1}, ""), {"a": 1})

    def test_records_normalizes_broker_shapes(self):
        self.assertEqual(records_from({"rows": "null"}, "rows"), [])
        self.assertEqual(records_from({"rows": {"a": 1}}, "rows"), [{"a": 1}])
        self.assertEqual(records_from({"rows": [{"a": 1}, 7]}, "rows"), [{"a": 1}])
        self.assertEqual(records_from([{"a": 1}], ""), [{"a": 1}])


class SpecValidationTests(unittest.TestCase):
    def test_a_valid_spec_parses(self):
        spec = parse_spec(VALID_SPEC)
        self.assertEqual(spec.name, "myprop")
        self.assertIs(spec.data_class, DataClass.LIVE)
        # The trailing slash is normalised away so paths never double up.
        self.assertEqual(spec.base_url, "https://api.example.com")

    def test_a_spec_needs_a_name_and_base_url(self):
        with self.assertRaises(SpecError):
            parse_spec({"base_url": "https://x"})
        with self.assertRaises(SpecError):
            parse_spec({"name": "x"})

    def test_a_spec_needs_at_least_one_endpoint(self):
        with self.assertRaises(SpecError):
            parse_spec({"name": "x", "base_url": "https://x"})

    def test_unknown_field_names_are_rejected(self):
        bad = {
            "name": "x",
            "base_url": "https://x",
            "positions": {"path": "/p", "fields": {"tiker": "t"}},
        }
        with self.assertRaises(SpecError) as caught:
            parse_spec(bad)
        # The message has to name the typo, or the spec author is guessing.
        self.assertIn("tiker", str(caught.exception))

    def test_auth_must_name_an_env_var_not_hold_a_secret(self):
        bad = {
            "name": "x",
            "base_url": "https://x",
            "auth": {"type": "bearer", "token": "hunter2"},
            "positions": {"path": "/p"},
        }
        with self.assertRaises(SpecError):
            parse_spec(bad)

    def test_unknown_auth_type_is_rejected(self):
        bad = {
            "name": "x",
            "base_url": "https://x",
            "auth": {"type": "magic", "token_env": "T"},
            "positions": {"path": "/p"},
        }
        with self.assertRaises(SpecError):
            parse_spec(bad)

    def test_unknown_trade_mode_is_rejected(self):
        bad = {
            "name": "x",
            "base_url": "https://x",
            "trades": {"path": "/t", "mode": "guess"},
        }
        with self.assertRaises(SpecError):
            parse_spec(bad)

    def test_an_endpoint_needs_a_path(self):
        with self.assertRaises(SpecError):
            parse_spec({"name": "x", "base_url": "https://x", "positions": {"records": "a"}})

    def test_the_bundled_example_is_itself_valid(self):
        self.assertEqual(parse_spec(EXAMPLE_SPEC).name, "example")

    def test_param_placeholders_resolve(self):
        endpoint = Endpoint(path="/t", params={"start": "{since}", "fixed": "1"})
        self.assertEqual(
            endpoint.resolved_params(since="2026-08-01"), {"start": "2026-08-01", "fixed": "1"}
        )


class SpecLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "feeds.d")
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, payload):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_specs_load_from_a_directory(self):
        self.write("a.json", VALID_SPEC)
        specs, errors = load_specs(self.dir)
        self.assertEqual([spec.name for spec in specs], ["myprop"])
        self.assertEqual(errors, [])

    def test_a_broken_spec_is_reported_not_raised(self):
        self.write("good.json", VALID_SPEC)
        self.write("bad.json", {"name": "bad"})
        specs, errors = load_specs(self.dir)

        # One bad file must not stop the others from loading.
        self.assertEqual([spec.name for spec in specs], ["myprop"])
        self.assertEqual(len(errors), 1)
        self.assertIn("bad.json", errors[0][0])

    def test_unparseable_json_is_reported(self):
        with open(os.path.join(self.dir, "junk.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        specs, errors = load_specs(self.dir)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)

    def test_duplicate_names_are_refused(self):
        self.write("a.json", VALID_SPEC)
        self.write("b.json", {**VALID_SPEC, "base_url": "https://other.example.com"})
        specs, errors = load_specs(self.dir)

        self.assertEqual(len(specs), 1)
        self.assertTrue(any("duplicate" in message for _path, message in errors))

    def test_non_json_files_are_ignored(self):
        with open(os.path.join(self.dir, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello")
        self.assertEqual(load_specs(self.dir), ([], []))

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(load_specs("/nonexistent/feeds.d"), ([], []))

    def test_ensure_creates_the_directory_and_a_sample(self):
        target = os.path.join(self.tmp.name, "fresh")
        created = ensure_feeds_dir(target)
        self.assertTrue(os.path.isdir(created))
        # The sample must not be loaded as a live spec.
        self.assertEqual(load_specs(created), ([], []))

    def test_describe_summarises_a_spec(self):
        summary = describe(parse_spec(VALID_SPEC))
        self.assertIn("positions", summary)
        self.assertIn("MYPROP_TOKEN", summary)


class DeclarativeFeedTests(unittest.TestCase):
    def feed(self, opener, spec=None, environ=None):
        return DeclarativeFeed(
            parse_spec(spec or VALID_SPEC),
            environ=environ if environ is not None else {"MYPROP_TOKEN": "secret"},
            opener=opener,
        )

    def test_a_missing_token_refuses_construction(self):
        with self.assertRaises(FeedUnconfigured):
            self.feed(None, environ={})

    def test_positions_are_mapped_through_the_spec(self):
        opener = RecordingOpener({"/v1/positions": POSITIONS_RESPONSE})
        positions = self.feed(opener).fetch_positions()

        self.assertEqual(len(positions), 1)
        held = positions[0]
        self.assertEqual(held.symbol, "SPY260803C00550000")
        # The option multiplier is derived from the symbol, not declared.
        self.assertEqual(held.contract_multiplier, 100)
        self.assertAlmostEqual(held.unrealized, 300.0)

    def test_an_absent_mark_stays_none(self):
        response = {"data": {"rows": [{"ticker": "SPY", "qty": 1, "avg": 5.0}]}}
        opener = RecordingOpener({"/v1/positions": response})
        position = self.feed(opener).fetch_positions()[0]

        # Never defaulted to the entry price — that is the bug that pins a P&L
        # readout at zero while the position actually moves.
        self.assertIsNone(position.mark_price)
        self.assertIsNone(position.unrealized)

    def test_rows_missing_required_fields_are_skipped(self):
        response = {"data": {"rows": [{"ticker": "SPY"}, {"qty": 1, "avg": 2}]}}
        opener = RecordingOpener({"/v1/positions": response})
        self.assertEqual(self.feed(opener).fetch_positions(), [])

    def test_closed_trades_are_mapped(self):
        opener = RecordingOpener({"/v1/trades": TRADES_RESPONSE})
        trades = self.feed(opener).fetch_trades(_dt.date(2026, 8, 1), _dt.date(2026, 8, 3))

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 240.0)

    def test_trades_outside_the_window_are_dropped(self):
        opener = RecordingOpener({"/v1/trades": TRADES_RESPONSE})
        trades = self.feed(opener).fetch_trades(_dt.date(2026, 7, 1), _dt.date(2026, 7, 2))
        self.assertEqual(trades, [])

    def test_fills_mode_reconstructs_round_trips(self):
        spec = {
            **VALID_SPEC,
            "trades": {
                "path": "/v1/fills",
                "records": "data.rows",
                "mode": "fills",
                "fields": {
                    "symbol": "ticker",
                    "side": "side",
                    "quantity": "qty",
                    "price": "px",
                    "filled_at": "ts",
                },
            },
        }
        response = {
            "data": {
                "rows": [
                    {"ticker": "AAPL", "side": "buy", "qty": 10, "px": 100, "ts": "2026-08-03T14:00:00Z"},
                    {"ticker": "AAPL", "side": "sell", "qty": 10, "px": 104, "ts": "2026-08-03T15:00:00Z"},
                ]
            }
        }
        opener = RecordingOpener({"/v1/fills": response})
        trades = self.feed(opener, spec).fetch_trades(_dt.date(2026, 8, 1), _dt.date(2026, 8, 3))

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].realized, 40.0)

    def test_the_bearer_token_reaches_the_request(self):
        opener = RecordingOpener({"/v1/positions": POSITIONS_RESPONSE})
        self.feed(opener).fetch_positions()
        self.assertTrue(opener.requests)

    def test_query_auth_puts_the_key_in_the_url(self):
        spec = {**VALID_SPEC, "auth": {"type": "query", "param": "apikey", "token_env": "MYPROP_TOKEN"}}
        opener = RecordingOpener({"/v1/positions": POSITIONS_RESPONSE})
        self.feed(opener, spec).fetch_positions()
        self.assertIn("apikey=secret", opener.requests[0])

    def test_window_placeholders_reach_the_query_string(self):
        opener = RecordingOpener({"/v1/trades": TRADES_RESPONSE})
        self.feed(opener).fetch_trades(_dt.date(2026, 8, 1), _dt.date(2026, 8, 3))
        self.assertIn("start=2026-08-01", opener.requests[0])
        self.assertIn("end=2026-08-03", opener.requests[0])

    def test_a_transport_failure_raises_rather_than_returning_nothing(self):
        from lumisync.pixeldash.feeds.base import FeedError

        opener = RecordingOpener({"/v1/positions": urllib.error.URLError("down")})
        with self.assertRaises(FeedError):
            self.feed(opener).fetch_positions()

    def test_a_spec_with_no_positions_endpoint_returns_nothing(self):
        spec = {key: value for key, value in VALID_SPEC.items() if key != "positions"}
        self.assertEqual(self.feed(None, spec).fetch_positions(), [])


class RegistryIntegrationTests(unittest.TestCase):
    def test_declared_feeds_appear_alongside_the_built_ins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            with open(os.path.join(cfg.feeds_dir, "myprop.json"), "w", encoding="utf-8") as handle:
                json.dump(VALID_SPEC, handle)

            feeds, statuses = build_declarative_feeds(cfg)
            # No token in the environment, so it reports unconfigured rather
            # than vanishing.
            self.assertEqual(feeds, [])
            self.assertEqual(len(statuses), 1)
            self.assertIn("MYPROP_TOKEN", statuses[0].detail)

    def test_a_broken_spec_becomes_an_error_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            with open(os.path.join(cfg.feeds_dir, "bad.json"), "w", encoding="utf-8") as handle:
                json.dump({"name": "bad"}, handle)

            feeds, statuses = build_declarative_feeds(cfg)
            self.assertEqual(feeds, [])
            self.assertTrue(statuses and not statuses[0].ok)


class GoveeBridgeTests(unittest.TestCase):
    """The bridge reads a manifest and picks a scene. It never sees a frame."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, snap=None, events=()):
        return render(snap or busy_snapshot(), self.config, events=events).manifest()

    @staticmethod
    def bridge_config(scenes=None):
        return BridgeConfig(api_key="key", device="AA:BB", sku="H6631", scenes=scenes or {})

    def test_rgb_packs_into_one_integer(self):
        self.assertEqual(rgb_to_int((255, 0, 0)), 0xFF0000)
        self.assertEqual(rgb_to_int((0, 255, 0)), 0x00FF00)
        self.assertEqual(rgb_to_int((0, 0, 255)), 0x0000FF)

    def test_config_from_env_reads_scene_ids(self):
        built = BridgeConfig.from_env(
            {
                "GOVEE_API_KEY": "k",
                "GOVEE_DEVICE_ID": "d",
                "GOVEE_DEVICE_SKU": "H6631",
                "GOVEE_SCENE_WIN": "12",
                "GOVEE_SCENE_LOSS": "notanumber",
            }
        )
        self.assertTrue(built.configured)
        self.assertEqual(built.scenes, {"win": 12})

    def test_missing_credentials_are_named(self):
        built = BridgeConfig.from_env({"GOVEE_API_KEY": "k"})
        self.assertFalse(built.configured)
        self.assertIn("GOVEE_DEVICE_ID", built.missing)

    def test_state_follows_the_event_over_the_rotation(self):
        win = DashEvent(EventKind.WIN, "k", "ANOTHER WIN", "SPY", 100.0, MOMENT)
        self.assertEqual(state_from_manifest(self.manifest(events=[win])), "win")

        loss = DashEvent(EventKind.LOSS, "k", "MISSION FAILED", "SPY", -100.0, MOMENT)
        self.assertEqual(state_from_manifest(self.manifest(events=[loss])), "loss")

    def test_state_reads_the_setup_and_error_cards(self):
        from lumisync.pixeldash.models import FeedStatus

        unset = snapshot(feeds=[FeedStatus.unconfigured("tradier", "no token")])
        self.assertEqual(state_from_manifest(self.manifest(unset)), "setup")

        down = snapshot(feeds=[FeedStatus.error("tradier", "HTTP 503")])
        self.assertEqual(state_from_manifest(self.manifest(down)), "feed_down")

    def test_an_unmapped_state_falls_back_to_flat(self):
        self.assertEqual(state_from_manifest({}), "flat")

    def test_an_unconfigured_bridge_reports_what_is_missing(self):
        self.assertIn("GOVEE_API_KEY", GoveeBridge(BridgeConfig()).apply("green"))

    def test_a_mapped_scene_is_activated(self):
        sent = []

        def opener(request, timeout=None):
            sent.append(json.loads(request.data.decode()))
            return _ok_response()

        bridge = GoveeBridge(self.bridge_config({"green": 42}), opener=opener)
        message = bridge.apply("green")

        self.assertIn("DIY scene 42", message)
        capabilities = [item["payload"]["capability"]["instance"] for item in sent]
        self.assertIn("diyScene", capabilities)

    def test_without_a_mapping_it_falls_back_to_one_colour(self):
        sent = []

        def opener(request, timeout=None):
            sent.append(json.loads(request.data.decode()))
            return _ok_response()

        message = GoveeBridge(self.bridge_config(), opener=opener).apply("green")
        self.assertIn("no DIY scene mapped", message)
        self.assertIn("colorRgb", [item["payload"]["capability"]["instance"] for item in sent])

    def test_an_unchanged_state_is_not_re_sent(self):
        sent = []

        def opener(request, timeout=None):
            sent.append(json.loads(request.data.decode()))
            return _ok_response()

        bridge = GoveeBridge(self.bridge_config({"green": 42}), opener=opener)
        bridge.apply("green")
        before = len(sent)
        message = bridge.apply("green")

        # Govee rate-limits per account; repeating an identical scene spends
        # that budget for no visible change.
        self.assertEqual(len(sent), before)
        self.assertIn("unchanged", message)

    def test_a_changed_state_is_sent(self):
        sent = []

        def opener(request, timeout=None):
            sent.append(json.loads(request.data.decode()))
            return _ok_response()

        bridge = GoveeBridge(self.bridge_config({"green": 42, "red": 43}), opener=opener)
        bridge.apply("green")
        before = len(sent)
        bridge.apply("red")
        self.assertGreater(len(sent), before)

    def test_scene_discovery_failure_is_survivable(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError("nope")

        self.assertEqual(GoveeBridge(self.bridge_config(), opener=opener).discover_scenes(), {})

    def test_the_runtime_package_stays_cloud_free(self):
        # LumiSync promises direct LAN control with no cloud dependency. The
        # bridge is a separate process precisely so that stays true.
        import lumisync.pixeldash.sinks as sinks_package

        self.assertFalse(
            hasattr(sinks_package, "govee_cloud"),
            "a vendor-cloud sink must not live inside the shipped package",
        )


def _ok_response():
    import io

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

    return Response(json.dumps({"code": 200, "msg": "success"}).encode())


if __name__ == "__main__":
    unittest.main()
