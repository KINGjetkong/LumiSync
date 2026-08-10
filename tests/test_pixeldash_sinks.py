"""Sink behaviour: capability probing, frame reduction, and honest reporting."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from lumisync.drivers.base import DeviceCapabilities
from lumisync.pixeldash.render.canvas import Frame
from lumisync.pixeldash.render.palette import GREEN, RED
from lumisync.pixeldash.render.pipeline import render
from lumisync.pixeldash.sinks.base import Sink, SinkReport, close_all, publish_all
from lumisync.pixeldash.sinks.files import FileSink
from lumisync.pixeldash.sinks.matrix import (
    MatrixSink,
    PanelMode,
    _fit_to_matrix,
    average_color,
    dedupe,
    limit,
    probe,
    to_segments,
)

from pixeldash_fixtures import busy_snapshot, config


class FakeAdapter:
    """Records what the sink asked the hardware to do."""

    def __init__(self, capabilities: DeviceCapabilities, *, pixel: bool = False) -> None:
        self._capabilities = capabilities
        self.grids = []
        self.segments = []
        self.colors = []
        self.brightness = None
        self.streams = 0
        if pixel:
            self.draw_grid = self._draw_grid

    @property
    def capabilities(self) -> DeviceCapabilities:
        return self._capabilities

    def _draw_grid(self, grid, clear=True):
        self.grids.append(grid)

    def set_segments(self, colors):
        self.segments.append(list(colors))

    def set_color(self, r, g, b):
        self.colors.append((r, g, b))

    def set_brightness(self, percent):
        self.brightness = percent

    def begin_stream(self):
        self.streams += 1

    def end_stream(self):
        pass

    def close(self):
        pass


MATRIX_CAPS = DeviceCapabilities(
    transport="ble", segment_count=1024, supports_segments=True, matrix_size=(32, 32)
)
STRIP_CAPS = DeviceCapabilities(transport="lan", segment_count=10, supports_segments=True)
BULB_CAPS = DeviceCapabilities(transport="lan", segment_count=1, supports_segments=False)
DEAD_CAPS = DeviceCapabilities(
    transport="lan", segment_count=0, supports_segments=False, supports_color=False
)


class ProbeTests(unittest.TestCase):
    def test_a_matrix_driver_resolves_to_per_pixel(self):
        capability = probe(FakeAdapter(MATRIX_CAPS, pixel=True))
        self.assertIs(capability.mode, PanelMode.PIXEL)
        self.assertTrue(capability.full_resolution)

    def test_a_matrix_without_a_draw_surface_falls_back_to_segments(self):
        # This is the honest answer for a device whose capabilities advertise a
        # grid but whose driver has no way to address it.
        capability = probe(FakeAdapter(MATRIX_CAPS, pixel=False))
        self.assertIs(capability.mode, PanelMode.SEGMENTS)
        self.assertFalse(capability.full_resolution)

    def test_a_zone_strip_resolves_to_segments(self):
        self.assertIs(probe(FakeAdapter(STRIP_CAPS)).mode, PanelMode.SEGMENTS)

    def test_a_single_colour_device_resolves_to_ambient(self):
        capability = probe(FakeAdapter(BULB_CAPS))
        self.assertIs(capability.mode, PanelMode.AMBIENT)
        self.assertFalse(capability.full_resolution)

    def test_a_device_with_no_colour_control_is_unusable(self):
        self.assertIs(probe(FakeAdapter(DEAD_CAPS)).mode, PanelMode.NONE)

    def test_a_broken_adapter_does_not_raise(self):
        class Broken:
            @property
            def capabilities(self):
                raise RuntimeError("transport gone")

        self.assertIs(probe(Broken()).mode, PanelMode.NONE)


class FrameReductionTests(unittest.TestCase):
    @staticmethod
    def solid(color) -> Frame:
        frame = Frame(4, 4)
        frame.rect(0, 0, 4, 4, color)
        return frame

    def test_identical_runs_collapse(self):
        frames = [self.solid(GREEN)] * 5 + [self.solid(RED)] * 3
        self.assertEqual([count for _frame, count in dedupe(frames)], [5, 3])

    def test_dedupe_preserves_total_duration(self):
        frames = [self.solid(GREEN)] * 4 + [self.solid(RED)] * 2
        self.assertEqual(sum(count for _frame, count in dedupe(frames)), len(frames))

    def test_limit_caps_the_frame_count_but_keeps_the_runtime(self):
        pairs = [(self.solid(GREEN), 2) for _ in range(20)]
        capped = limit(pairs, 5)
        self.assertEqual(len(capped), 5)
        self.assertEqual(
            sum(count for _frame, count in capped), sum(count for _frame, count in pairs)
        )

    def test_limit_is_a_no_op_below_the_cap(self):
        pairs = [(self.solid(GREEN), 1), (self.solid(RED), 1)]
        self.assertIs(limit(pairs, 10), pairs)


class ColourReductionTests(unittest.TestCase):
    def test_segments_slice_left_to_right(self):
        frame = Frame(8, 4)
        frame.rect(0, 0, 4, 4, GREEN)
        frame.rect(4, 0, 4, 4, RED)
        segments = to_segments(frame, 2)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0], GREEN)
        self.assertEqual(segments[1], RED)

    def test_segment_count_is_always_honoured(self):
        frame = Frame(52, 32)
        for count in (1, 7, 10, 255):
            self.assertEqual(len(to_segments(frame, count)), count)

    def test_average_ignores_the_black_background(self):
        frame = Frame(16, 16)  # mostly black
        frame.rect(0, 0, 2, 2, GREEN)
        self.assertEqual(average_color(frame), GREEN)

    def test_an_entirely_black_frame_averages_to_black(self):
        self.assertEqual(average_color(Frame(8, 8)), (0, 0, 0))

    def test_fitting_to_a_smaller_matrix_drops_pixels_without_blurring(self):
        frame = Frame(52, 32)
        frame.rect(0, 0, 26, 32, GREEN)
        frame.rect(26, 0, 26, 32, RED)
        grid = _fit_to_matrix(frame, (16, 16))

        self.assertEqual(len(grid), 16)
        self.assertEqual(len(grid[0]), 16)
        self.assertEqual(grid[0][0], GREEN)
        self.assertEqual(grid[0][15], RED)

    def test_fitting_to_the_same_size_is_identity(self):
        frame = Frame(8, 8)
        frame.set_pixel(3, 3, GREEN)
        self.assertEqual(_fit_to_matrix(frame, (8, 8))[3][3], GREEN)


class MatrixSinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)
        self.result = render(busy_snapshot(), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def sink_with(self, adapter) -> MatrixSink:
        sink = MatrixSink({"ip": "10.0.0.1"}, max_frames=4)
        sink._adapter = adapter
        sink._capability = probe(adapter)
        return sink

    def test_a_pixel_panel_reports_a_clean_success(self):
        adapter = FakeAdapter(MATRIX_CAPS, pixel=True)
        sink = self.sink_with(adapter)
        try:
            report = sink.publish(self.result)
            self.assertTrue(report.ok)
            self.assertFalse(report.degraded)
        finally:
            sink.close()

    def test_a_zone_strip_reports_degraded_rather_than_success(self):
        sink = self.sink_with(FakeAdapter(STRIP_CAPS))
        try:
            report = sink.publish(self.result)
            self.assertTrue(report.ok)
            self.assertTrue(report.degraded, "a strip cannot show a dashboard; say so")
            self.assertIn("zone", report.detail)
        finally:
            sink.close()

    def test_a_single_colour_device_reports_degraded(self):
        sink = self.sink_with(FakeAdapter(BULB_CAPS))
        try:
            report = sink.publish(self.result)
            self.assertTrue(report.ok)
            self.assertTrue(report.degraded)
        finally:
            sink.close()

    def test_an_unusable_device_fails_loudly(self):
        sink = self.sink_with(FakeAdapter(DEAD_CAPS))
        try:
            self.assertFalse(sink.publish(self.result).ok)
        finally:
            sink.close()

    def test_playback_actually_reaches_the_device(self):
        adapter = FakeAdapter(MATRIX_CAPS, pixel=True)
        sink = self.sink_with(adapter)
        try:
            sink.publish(self.result)
            sink._thread.join(timeout=3.0)
            self.assertTrue(adapter.grids, "no frames were drawn")
            self.assertEqual(adapter.brightness, self.config.brightness)
        finally:
            sink.close()

    def test_frames_sent_to_a_panel_are_capped(self):
        adapter = FakeAdapter(MATRIX_CAPS, pixel=True)
        sink = MatrixSink({"ip": "10.0.0.1"}, max_frames=3, min_frame_ms=20)
        sink._adapter = adapter
        sink._capability = probe(adapter)
        try:
            report = sink.publish(self.result)
            self.assertIn("3 distinct frames", report.detail)
        finally:
            sink.close()


class FileSinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)
        self.result = render(busy_snapshot(), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_publishing_writes_a_stable_filename(self):
        report = FileSink(self.tmp.name).publish(self.result)
        self.assertTrue(report.ok)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "dashboard.gif")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "dashboard.json")))

    def test_republishing_overwrites_in_place(self):
        sink = FileSink(self.tmp.name)
        sink.publish(self.result)
        sink.publish(self.result)
        gifs = [name for name in os.listdir(self.tmp.name) if name.endswith(".gif")]
        self.assertEqual(gifs, ["dashboard.gif"])

    def test_history_keeps_a_digest_named_copy(self):
        sink = FileSink(self.tmp.name, keep_history=True)
        report = sink.publish(self.result)
        self.assertIn("history", report.artifacts)
        self.assertTrue(os.path.exists(report.artifacts["history"]))
        self.assertIn(self.result.digest, report.artifacts["history"])

    def test_an_unwritable_directory_reports_failure(self):
        report = FileSink("/proc/nonexistent/pixeldash").publish(self.result)
        self.assertFalse(report.ok)

    def test_the_manifest_records_the_plan_source(self):
        FileSink(self.tmp.name).publish(self.result)
        with open(os.path.join(self.tmp.name, "dashboard.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn(manifest["plan_source"], ("rules", "cache", "hook"))


class FanOutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.result = render(busy_snapshot(), config(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_raising_sink_does_not_stop_the_others(self):
        class Exploding(Sink):
            name = "boom"

            def publish(self, result):
                raise RuntimeError("nope")

        reports = publish_all([Exploding(), FileSink(self.tmp.name)], self.result)
        self.assertEqual(len(reports), 2)
        self.assertFalse(reports[0].ok)
        self.assertTrue(reports[1].ok)

    def test_close_all_tolerates_a_raising_sink(self):
        class Stubborn(Sink):
            name = "stubborn"

            def publish(self, result):
                return SinkReport(sink=self.name, ok=True)

            def close(self):
                raise RuntimeError("nope")

        close_all([Stubborn()])  # must not raise

    def test_report_summary_distinguishes_degraded_from_ok(self):
        self.assertIn("degraded", SinkReport("p", True, degraded=True).summary)
        self.assertIn("FAILED", SinkReport.failure("p", "gone").summary)
        self.assertIn("ok", SinkReport("p", True).summary)


if __name__ == "__main__":
    unittest.main()
