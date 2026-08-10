"""Rendering: determinism, layout safety and the failure cards."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from lumisync.pixeldash import format as fmt
from lumisync.pixeldash.events import DashEvent, EventKind
from lumisync.pixeldash.models import KNOWN_TARGETS, FeedStatus
from lumisync.pixeldash.render import font, metrics, planner, scenes
from lumisync.pixeldash.render.canvas import Frame, Sprite
from lumisync.pixeldash.render.gif import decode_frames, encode, unique_colors
from lumisync.pixeldash.render.palette import GREEN, RED, heat_color, pnl_color
from lumisync.pixeldash.render.pipeline import render, snapshot_digest, write_bundle

from pixeldash_fixtures import MOMENT, busy_snapshot, config, snapshot, trade


class FontTests(unittest.TestCase):
    def test_every_glyph_row_is_rectangular(self):
        # build_font raises on ragged glyphs, so both faces existing at import
        # time already proves this; the assertion documents the invariant.
        for face in font.FONTS:
            for char, offsets in face.glyphs.items():
                width = face.glyph_width(char)
                for x, y in offsets:
                    self.assertLess(x, width, f"{face.name} {char!r} overflows its width")
                    self.assertLess(y, face.height, f"{face.name} {char!r} overflows its height")

    def test_a_ragged_glyph_is_refused_at_build_time(self):
        with self.assertRaises(ValueError):
            font.build_font("bad", {"A": ("##", "#"), "?": ("#", "#")})

    def test_glyphs_disagreeing_on_height_are_refused(self):
        with self.assertRaises(ValueError):
            font.build_font("bad", {"A": ("#", "#"), "?": ("#", "#", "#")})

    def test_both_faces_cover_the_same_characters(self):
        # A string that renders on the panel must render on the screen too.
        self.assertEqual(set(font.SMALL.glyphs), set(font.LARGE.glyphs))

    def test_m_n_and_w_are_distinct_shapes(self):
        # The whole reason the small font is variable-width. If these ever
        # collapse back into each other, OPEN reads as OPEM on the panel.
        for face in font.FONTS:
            self.assertNotEqual(face.glyphs["M"], face.glyphs["N"], face.name)
            self.assertNotEqual(face.glyphs["W"], face.glyphs["M"], face.name)
            self.assertNotEqual(face.glyphs["N"], face.glyphs["H"], face.name)

    def test_the_large_face_is_taller_and_wider(self):
        self.assertGreater(font.LARGE.height, font.SMALL.height)
        self.assertGreater(font.LARGE.text_width("SPY"), font.SMALL.text_width("SPY"))

    def test_module_level_helpers_use_the_panel_face(self):
        self.assertEqual(font.text_width("SPY"), font.SMALL.text_width("SPY"))
        self.assertEqual(font.text_height(), font.SMALL.height)

    def test_text_width_accounts_for_wide_glyphs(self):
        self.assertGreater(font.text_width("NN"), font.text_width("II"))

    def test_fit_truncates_to_the_available_width(self):
        self.assertEqual(font.fit("ABCDEFGH", 0), "")
        fitted = font.fit("ABCDEFGH", 20)
        self.assertLessEqual(font.text_width(fitted), 20)

    def test_wrap_splits_on_word_boundaries(self):
        self.assertEqual(font.wrap("MISSION FAILED", 50), ["MISSION", "FAILED"])
        self.assertEqual(font.wrap("FEED DOWN", 50), ["FEED DOWN"])

    def test_wrap_never_exceeds_the_line_budget(self):
        rows = font.wrap("ONE TWO THREE FOUR FIVE SIX", 30, lines=2)
        self.assertLessEqual(len(rows), 2)
        for row in rows:
            self.assertLessEqual(font.text_width(row), 30)

    def test_unknown_characters_fall_back_rather_than_raise(self):
        self.assertEqual(font.normalize("A€B"), "A?B")


class FormatTests(unittest.TestCase):
    def test_missing_values_render_as_a_dash(self):
        self.assertEqual(fmt.money(None), "--")
        self.assertEqual(fmt.percent(None), "--")
        self.assertEqual(fmt.price(None), "--")
        self.assertEqual(fmt.clock(None), "--")

    def test_money_compacts_large_numbers(self):
        self.assertEqual(fmt.money(1240.0), "+1240")
        self.assertEqual(fmt.money(-12345.0), "-12.3K")
        self.assertEqual(fmt.money(2_500_000.0), "+2.5M")
        self.assertEqual(fmt.money(0.0), "0")

    def test_zero_is_not_signed(self):
        self.assertNotIn("+", fmt.money(0.0))

    def test_short_symbol_compresses_occ(self):
        self.assertEqual(fmt.short_symbol("SPY260803C00550000"), "SPY550C")
        self.assertEqual(fmt.short_symbol("AAPL"), "AAPL")

    def test_fit_scale_backs_off_until_it_fits(self):
        self.assertEqual(fmt.fit_scale("+1240", 200, preferred=2), 2)
        self.assertEqual(fmt.fit_scale("+1240", 25, preferred=2), 1)


class CanvasTests(unittest.TestCase):
    def test_drawing_outside_the_frame_is_clipped_not_wrapped(self):
        frame = Frame(8, 8)
        frame.set_pixel(-1, -1, RED)
        frame.set_pixel(99, 99, RED)
        frame.rect(6, 6, 10, 10, GREEN)
        self.assertEqual(frame.get_pixel(0, 0), (0, 0, 0))
        self.assertEqual(frame.get_pixel(7, 7), GREEN)

    def test_transparent_sprite_cells_leave_the_background(self):
        frame = Frame(4, 2)
        frame.rect(0, 0, 4, 2, RED)
        frame.blit(Sprite(rows=("..", "BB"), colors={".": None, "B": GREEN}), 0, 0)
        self.assertEqual(frame.get_pixel(0, 0), RED)
        self.assertEqual(frame.get_pixel(0, 1), GREEN)

    def test_upscale_is_nearest_neighbour(self):
        frame = Frame(2, 1)
        frame.set_pixel(0, 0, RED)
        frame.set_pixel(1, 0, GREEN)
        scaled = frame.upscale(3)
        self.assertEqual(scaled.shape, (3, 6, 3))
        self.assertEqual(tuple(scaled[0, 0]), RED)
        self.assertEqual(tuple(scaled[2, 5]), GREEN)

    def test_frames_compare_by_content(self):
        first, second = Frame(4, 4), Frame(4, 4)
        self.assertEqual(first, second)
        second.set_pixel(0, 0, RED)
        self.assertNotEqual(first, second)


class PaletteTests(unittest.TestCase):
    def test_pnl_colour_follows_the_sign(self):
        self.assertEqual(pnl_color(1.0), GREEN)
        self.assertEqual(pnl_color(-1.0), RED)
        self.assertNotIn(pnl_color(0.0), (GREEN, RED))

    def test_heat_ramps_are_signed_and_clamped(self):
        self.assertEqual(heat_color(9), heat_color(4))
        self.assertEqual(heat_color(-9), heat_color(-4))
        self.assertNotEqual(heat_color(3), heat_color(-3))


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)
        self.snapshot = busy_snapshot()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_snapshot_renders_identical_bytes(self):
        first = render(self.snapshot, self.config)
        second = render(self.snapshot, self.config)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.gif_bytes(), second.gif_bytes())

    def test_a_changed_number_changes_the_digest(self):
        other = snapshot(trades=[trade(1.0)])
        self.assertNotEqual(snapshot_digest(self.snapshot), snapshot_digest(other))

    def test_event_keys_participate_in_the_digest(self):
        event = DashEvent(EventKind.WIN, "k", "ANOTHER WIN", "SPY", 100.0, MOMENT)
        with_event = render(self.snapshot, self.config, events=[event])
        without = render(self.snapshot, self.config)
        self.assertNotEqual(with_event.digest, without.digest)
        self.assertGreater(with_event.frame_count, without.frame_count)

    def test_float_noise_below_a_hundredth_of_a_cent_does_not_change_output(self):
        from dataclasses import replace

        drifted = replace(self.snapshot, equity=1000.000001)
        base = replace(self.snapshot, equity=1000.0)
        self.assertEqual(snapshot_digest(base), snapshot_digest(drifted))

    def test_sparkles_stay_inside_the_gif_palette(self):
        event = DashEvent(EventKind.WIN, "k", "ANOTHER WIN", "SPY", 100.0, MOMENT)
        result = render(self.snapshot, self.config, events=[event])
        self.assertLessEqual(len(unique_colors(result.frames)), 256)


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _context(self, snap, target_name="H6631"):
        import random

        self.config.target = KNOWN_TARGETS[target_name]
        return scenes.build_context(
            snap, self.config, planner.rule_plan(snap, self.config), random.Random(1)
        )

    def test_every_scene_renders_on_every_known_panel(self):
        snap = busy_snapshot()
        for target_name in KNOWN_TARGETS:
            context = self._context(snap, target_name)
            for scene_id in scenes.available_scenes():
                frames = scenes.compose(context, scene_id)
                self.assertTrue(frames, f"{scene_id} on {target_name} produced nothing")
                self.assertEqual(frames[0].cols, KNOWN_TARGETS[target_name].cols)
                self.assertEqual(frames[0].rows, KNOWN_TARGETS[target_name].rows)

    def test_empty_account_still_renders_the_daily_card(self):
        context = self._context(snapshot())
        self.assertTrue(scenes.compose(context, "daily"))

    def test_positions_page_when_there_are_more_than_fit(self):
        from pixeldash_fixtures import position

        many = snapshot(positions=[position(symbol=f"SPY26080{i}C00550000") for i in range(7)])
        frames = scenes.compose(self._context(many), "positions")
        # Paging means the scene is no longer a single repeated still.
        self.assertGreater(len({frame.tobytes() for frame in frames}), 1)

    def test_event_scenes_render_for_every_kind(self):
        context = self._context(busy_snapshot())
        for kind in EventKind:
            event = DashEvent(kind, "k", kind.value.upper(), "DETAIL", 12.0, MOMENT)
            self.assertTrue(scenes.scene_event(context, event))

    def test_marquee_leaves_short_text_alone(self):
        self.assertEqual(scenes.marquee("HI", 50, 3), "HI")
        self.assertNotEqual(scenes.marquee("A VERY LONG STATUS LINE", 20, 0), "A VERY LONG STATUS LINE")


class FailureCardTests(unittest.TestCase):
    """A broken feed must produce a visible card, never a plausible blank one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unconfigured_broker_routes_to_the_setup_card(self):
        snap = snapshot(feeds=[FeedStatus.unconfigured("tradier", "missing token")])
        plan = planner.rule_plan(snap, self.config)
        self.assertEqual(plan.scenes, ("setup",))

    def test_failed_feed_puts_the_error_card_first(self):
        snap = snapshot(
            trades=[trade(100.0)],
            feeds=[FeedStatus.error("tradier", "HTTP 503")],
        )
        plan = planner.rule_plan(snap, self.config)
        self.assertEqual(plan.scenes[0], "error")

    def test_a_healthy_snapshot_shows_no_failure_card(self):
        plan = planner.rule_plan(busy_snapshot(), self.config)
        self.assertNotIn("error", plan.scenes)
        self.assertNotIn("setup", plan.scenes)

    def test_render_succeeds_even_with_every_feed_down(self):
        snap = snapshot(feeds=[FeedStatus.error("tradier", "unreachable")])
        result = render(snap, self.config)
        self.assertGreater(result.frame_count, 0)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_plan_digest_ignores_small_pnl_drift(self):
        first = snapshot(trades=[trade(1000.0)])
        second = snapshot(trades=[trade(1001.0)])
        self.assertEqual(planner.plan_digest(first), planner.plan_digest(second))

    def test_the_plan_digest_reacts_to_a_sign_flip(self):
        green = snapshot(trades=[trade(1000.0)])
        red = snapshot(trades=[trade(-1000.0)])
        self.assertNotEqual(planner.plan_digest(green), planner.plan_digest(red))

    def test_scenes_are_dropped_when_they_have_nothing_to_show(self):
        plan = planner.rule_plan(snapshot(), self.config)
        self.assertNotIn("positions", plan.scenes)
        self.assertNotIn("calendar", plan.scenes)

    def test_a_hook_cannot_invent_a_scene(self):
        baseline = planner.rule_plan(busy_snapshot(), self.config)
        resolved = planner.validate({"scenes": ["rm -rf", "daily"]}, baseline)
        self.assertEqual(resolved.scenes, ("daily",))

    def test_a_hook_headline_is_clamped_and_sanitised(self):
        baseline = planner.rule_plan(busy_snapshot(), self.config)
        resolved = planner.validate({"headline": "<script>alert(1)</script>"}, baseline)
        self.assertLessEqual(len(resolved.headline), planner.MAX_HEADLINE)
        self.assertNotIn("<", resolved.headline)

    def test_a_hook_cannot_set_an_invisible_accent(self):
        baseline = planner.rule_plan(busy_snapshot(), self.config)
        resolved = planner.validate({"accent": [1, 1, 1]}, baseline)
        self.assertEqual(resolved.accent, baseline.accent)

    def test_garbage_from_a_hook_falls_back_to_the_baseline(self):
        baseline = planner.rule_plan(busy_snapshot(), self.config)
        self.assertIs(planner.validate(None, baseline), baseline)
        self.assertIs(planner.validate("nope", baseline), baseline)

    def test_a_raising_hook_does_not_break_the_render(self):
        def hook(_context):
            raise RuntimeError("model unavailable")

        resolved = planner.plan(busy_snapshot(), self.config, hook=hook)
        self.assertEqual(resolved.source, "rules")

    def test_a_plan_round_trips_through_the_cache(self):
        baseline = planner.rule_plan(busy_snapshot(), self.config)
        planner.save_plan(self.config.plan_cache_dir, baseline)
        loaded = planner.load_plan(self.config.plan_cache_dir, baseline.digest)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.scenes, baseline.scenes)
        self.assertEqual(loaded.source, "cache")

    def test_the_planner_context_carries_no_dollar_amounts(self):
        snap = busy_snapshot()
        payload = json.dumps(planner.planner_context(snap, planner.rule_plan(snap, self.config)))
        for amount in ("240", "410", "1.25", "1.55"):
            self.assertNotIn(amount, payload)


class GifTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_encoding_an_empty_animation_is_refused(self):
        with self.assertRaises(ValueError):
            encode([])

    def test_frames_survive_a_gif_round_trip(self):
        frame = Frame(8, 4)
        frame.rect(0, 0, 4, 4, GREEN)
        frame.rect(4, 0, 4, 4, RED)
        decoded = decode_frames(encode([frame]))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].get_pixel(0, 0), GREEN)
        self.assertEqual(decoded[0].get_pixel(7, 3), RED)

    def test_write_bundle_produces_gif_still_and_manifest(self):
        result = render(busy_snapshot(), self.config)
        paths = write_bundle(result, self.tmp.name)

        for key in ("gif", "still", "manifest"):
            self.assertTrue(os.path.exists(paths[key]), key)

        with open(paths["manifest"], encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["digest"], result.digest)
        self.assertEqual(manifest["frames"], result.frame_count)
        self.assertEqual(manifest["scenes"], list(result.scene_ids))

    def test_upscaling_multiplies_the_pixel_dimensions(self):
        result = render(busy_snapshot(), self.config)
        decoded = decode_frames(result.gif_bytes(scale=3))
        self.assertEqual(decoded[0].cols, self.config.target.cols * 3)


if __name__ == "__main__":
    unittest.main()


class MetricsTests(unittest.TestCase):
    """Layout has to come from the target, not from constants in the composer."""

    def test_the_panel_keeps_the_small_face(self):
        chosen = metrics.metrics_for(KNOWN_TARGETS["H6631"])
        self.assertIs(chosen.face, font.SMALL)

    def test_screen_targets_earn_the_large_face(self):
        for name in ("screen", "screen-xl"):
            self.assertIs(metrics.metrics_for(KNOWN_TARGETS[name]).face, font.LARGE)

    def test_tiny_panels_never_get_the_large_face(self):
        for name in ("16x16", "16x32", "32x32", "64x32"):
            self.assertIs(metrics.metrics_for(KNOWN_TARGETS[name]).face, font.SMALL)

    # The hero measurements only govern the full layout. Compact targets take
    # scene_daily's stripped-down branch, which sizes per value against the raw
    # panel width — a 16x16 grid cannot fit "-99.9K" at any scale, and pretending
    # the metric could satisfy that would be a lie about the hardware.
    def test_the_hero_number_leaves_room_for_the_record_line(self):
        for target in KNOWN_TARGETS.values():
            chosen = metrics.metrics_for(target)
            if chosen.compact:
                continue
            needed = (
                chosen.body_top
                + chosen.face.height * chosen.hero_scale
                + 3
                + chosen.face.height
            )
            self.assertLessEqual(needed, target.rows, target.name)

    def test_the_hero_specimen_fits_the_width(self):
        for target in KNOWN_TARGETS.values():
            chosen = metrics.metrics_for(target)
            if chosen.compact:
                continue
            width = chosen.face.text_width(metrics.HERO_SPECIMEN, chosen.hero_scale)
            self.assertLessEqual(width, chosen.content_width + chosen.margin, target.name)

    def test_compact_targets_still_render_a_readable_number(self):
        # The compact branch truncates rather than overflowing, which is the
        # only honest option at 16 pixels wide.
        for name in ("16x16", "16x32"):
            chosen = metrics.metrics_for(KNOWN_TARGETS[name])
            self.assertTrue(chosen.compact)
            self.assertEqual(chosen.hero_scale, 1)

    def test_position_rows_fit_the_body(self):
        for target in KNOWN_TARGETS.values():
            chosen = metrics.metrics_for(target)
            if chosen.compact:
                continue
            rows = (target.rows - chosen.body_top) // chosen.row_pitch
            self.assertGreaterEqual(rows, 1, target.name)

    def test_a_denser_target_gets_bigger_measurements(self):
        panel = metrics.metrics_for(KNOWN_TARGETS["H6631"])
        screen = metrics.metrics_for(KNOWN_TARGETS["screen"])
        self.assertGreater(screen.row_pitch, panel.row_pitch)
        self.assertGreater(screen.cell_pitch, panel.cell_pitch)
        self.assertGreater(screen.line_height, panel.line_height)

    def test_content_area_stays_inside_the_target(self):
        for target in KNOWN_TARGETS.values():
            chosen = metrics.metrics_for(target)
            self.assertGreater(chosen.content_width, 0, target.name)
            self.assertLessEqual(chosen.right, target.cols, target.name)


class MultiTargetRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_target_override_changes_the_frame_size(self):
        result = render(busy_snapshot(), self.config, target=KNOWN_TARGETS["screen"])
        self.assertEqual(result.frames[0].cols, 104)
        self.assertEqual(result.frames[0].rows, 64)
        self.assertEqual(result.target.name, "screen")

    def test_the_target_is_folded_into_the_digest(self):
        panel = render(busy_snapshot(), self.config)
        screen = render(busy_snapshot(), self.config, target=KNOWN_TARGETS["screen"])
        self.assertNotEqual(panel.digest, screen.digest)

    def test_each_target_is_still_individually_deterministic(self):
        first = render(busy_snapshot(), self.config, target=KNOWN_TARGETS["screen"])
        second = render(busy_snapshot(), self.config, target=KNOWN_TARGETS["screen"])
        self.assertEqual(first.gif_bytes(), second.gif_bytes())

    def test_the_manifest_records_the_geometry(self):
        manifest = render(
            busy_snapshot(), self.config, target=KNOWN_TARGETS["screen"]
        ).manifest()
        self.assertEqual(manifest["target"], "screen")
        self.assertEqual(manifest["size"], [104, 64])

    def test_every_scene_renders_on_the_screen_targets(self):
        import random

        for name in ("screen", "screen-xl"):
            target = KNOWN_TARGETS[name]
            snap = busy_snapshot()
            context = scenes.build_context(
                snap,
                self.config,
                planner.rule_plan(snap, self.config),
                random.Random(1),
                target=target,
            )
            for scene_id in scenes.available_scenes():
                frames = scenes.compose(context, scene_id)
                self.assertTrue(frames, f"{scene_id} on {name}")
                self.assertEqual(frames[0].cols, target.cols)
