"""Push the dashboard onto a physical light or panel.

What actually reaches the hardware depends on what the hardware can do, and this
module is deliberately explicit about the difference:

``PIXEL``
    The device exposes a per-pixel drawing surface (LumiSync's iDotMatrix BLE
    driver implements one). The dashboard appears as drawn.
``SEGMENTS``
    The device accepts a per-zone colour stream — Govee's Razer/DreamView path.
    LumiSync caps that at 255 zones, so a 52x32 dashboard is reduced to a strip
    of averages. Recognisable as a mood, not readable as a dashboard.
``AMBIENT``
    Only a single colour. The panel becomes a status light: green day, red day,
    amber when a feed is down.
``NONE``
    Nothing usable.

On the Govee Gaming Pixel Light specifically: Govee's documented LAN API covers
power, brightness and one colour, with no published per-pixel surface, so this
sink resolves to ``AMBIENT`` there and says so. The full-resolution dashboard
for that panel goes out through :class:`~lumisync.pixeldash.sinks.files.FileSink`
as a GIF, which the panel's own app can import.

Frames are de-duplicated before playback. A rotation holds each scene for
seconds at a time, so 250 rendered frames usually collapse to a handful of
distinct images — which is the difference between a BLE panel keeping up and a
BLE panel falling over.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..render.canvas import Frame
from ..render.pipeline import RenderResult
from .base import Sink, SinkReport

RGB = Tuple[int, int, int]


class PanelMode(str, Enum):
    PIXEL = "pixel"
    SEGMENTS = "segments"
    AMBIENT = "ambient"
    NONE = "none"


@dataclass(frozen=True)
class PanelCapability:
    """The resolved output path for one device, and why."""

    mode: PanelMode
    reason: str
    segment_count: int = 0
    matrix_size: Optional[Tuple[int, int]] = None

    @property
    def full_resolution(self) -> bool:
        return self.mode is PanelMode.PIXEL

    def describe(self) -> str:
        return f"{self.mode.value} — {self.reason}"


def probe(adapter) -> PanelCapability:
    """Decide how much of a dashboard this adapter can actually show.

    Capability is read from the driver, never guessed from a model name. An
    adapter that grows a per-pixel path later is picked up automatically.
    """
    try:
        capabilities = adapter.capabilities
    except Exception as exc:
        return PanelCapability(PanelMode.NONE, f"capabilities unavailable: {exc}")

    if capabilities.is_matrix and hasattr(adapter, "draw_grid"):
        return PanelCapability(
            PanelMode.PIXEL,
            "driver exposes a per-pixel surface",
            segment_count=capabilities.segment_count,
            matrix_size=capabilities.matrix_size,
        )

    if capabilities.supports_segments and capabilities.segment_count > 1:
        return PanelCapability(
            PanelMode.SEGMENTS,
            f"per-zone stream only ({capabilities.segment_count} zones)",
            segment_count=capabilities.segment_count,
        )

    if capabilities.supports_color:
        return PanelCapability(PanelMode.AMBIENT, "single-colour control only")

    return PanelCapability(PanelMode.NONE, "device exposes no colour control")


# --- frame reduction ----------------------------------------------------

def dedupe(frames: Sequence[Frame]) -> List[Tuple[Frame, int]]:
    """Collapse runs of identical frames into ``(frame, repeats)`` pairs."""
    reduced: List[Tuple[Frame, int]] = []
    for frame in frames:
        if reduced and reduced[-1][0] == frame:
            previous, count = reduced[-1]
            reduced[-1] = (previous, count + 1)
        else:
            reduced.append((frame, 1))
    return reduced


def limit(pairs: List[Tuple[Frame, int]], maximum: int) -> List[Tuple[Frame, int]]:
    """Keep at most ``maximum`` distinct frames, preserving total duration.

    Dropped frames' repeats are folded into the frame that survives, so the
    animation still takes the same wall-clock time — it just steps in coarser
    increments.
    """
    if maximum <= 0 or len(pairs) <= maximum:
        return pairs

    step = len(pairs) / maximum
    kept: List[Tuple[Frame, int]] = []
    for index in range(maximum):
        start = int(round(index * step))
        end = int(round((index + 1) * step)) if index + 1 < maximum else len(pairs)
        bucket = pairs[start:max(end, start + 1)]
        total = sum(count for _frame, count in bucket)
        kept.append((bucket[0][0], total))
    return kept


def to_segments(frame: Frame, count: int) -> List[RGB]:
    """Reduce a frame to ``count`` colours by averaging vertical slices.

    A zone stream is linear, so the frame is sliced left-to-right and each slice
    averaged. Averaging in linear light would be more correct, but these values
    drive an ambient approximation, not a colour-managed display.
    """
    import numpy as np

    count = max(1, int(count))
    edges = np.linspace(0, frame.cols, count + 1).astype(int)
    colors: List[RGB] = []
    for index in range(count):
        start, end = edges[index], max(edges[index + 1], edges[index] + 1)
        slice_mean = frame.data[:, start:end].reshape(-1, 3).mean(axis=0)
        colors.append(tuple(int(round(channel)) for channel in slice_mean))
    return colors


def average_color(frame: Frame) -> RGB:
    """Mean colour of a frame, ignoring pure black.

    The dashboard is mostly black background; averaging that in would render
    every scene as "almost off". Excluding it makes the ambient mode track the
    content — green on a green day, red on a red one.
    """

    pixels = frame.data.reshape(-1, 3)
    lit = pixels[pixels.sum(axis=1) > 24]
    if not len(lit):
        return (0, 0, 0)
    return tuple(int(round(channel)) for channel in lit.mean(axis=0))


# --- sink ---------------------------------------------------------------

class MatrixSink(Sink):
    """Play a render on one LumiSync device.

    Playback runs on a daemon thread. Publishing a new render stops the previous
    animation before starting the next, so the panel never interleaves two
    dashboards.
    """

    name = "panel"

    def __init__(
        self,
        device: Dict[str, Any],
        *,
        brightness: int = 70,
        max_frames: int = 24,
        min_frame_ms: int = 200,
    ) -> None:
        self.device = dict(device)
        self.brightness = max(1, min(100, int(brightness)))
        self.max_frames = max(1, int(max_frames))
        # BLE panels redraw slowly; going faster than this just queues packets.
        self.min_frame_ms = max(20, int(min_frame_ms))

        self._adapter = None
        self._capability: Optional[PanelCapability] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._brightness_applied = False

    # --- device access ---
    def _ensure_adapter(self):
        if self._adapter is None:
            from ...drivers import pool

            self._adapter = pool.acquire(self.device)
            self._capability = probe(self._adapter)
        return self._adapter

    @property
    def capability(self) -> Optional[PanelCapability]:
        return self._capability

    def describe(self) -> str:
        try:
            self._ensure_adapter()
        except Exception as exc:
            return f"unavailable: {exc}"
        return self._capability.describe() if self._capability else "unknown"

    # --- publishing ---
    def publish(self, result: RenderResult) -> SinkReport:
        try:
            adapter = self._ensure_adapter()
        except Exception as exc:
            return SinkReport.failure(self.name, f"cannot reach device: {exc}")

        capability = self._capability or probe(adapter)
        if capability.mode is PanelMode.NONE:
            return SinkReport.failure(self.name, capability.reason)

        self._stop_playback()

        pairs = limit(dedupe(result.frames), self.max_frames)
        frame_ms = max(self.min_frame_ms, result.frame_ms)

        if not self._brightness_applied:
            try:
                adapter.set_brightness(self.brightness)
                self._brightness_applied = True
            except Exception:
                # Brightness is a nicety; losing it must not block the frames.
                pass

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._play,
            args=(adapter, capability, pairs, frame_ms, self._stop),
            name="pixeldash-panel",
            daemon=True,
        )
        self._thread.start()

        return SinkReport(
            sink=self.name,
            ok=True,
            degraded=not capability.full_resolution,
            detail=f"{capability.describe()}; {len(pairs)} distinct frames",
        )

    def _play(
        self,
        adapter,
        capability: PanelCapability,
        pairs: List[Tuple[Frame, int]],
        frame_ms: int,
        stop: threading.Event,
    ) -> None:
        """Loop the animation until asked to stop or the device errors out."""
        if not pairs:
            return
        try:
            adapter.begin_stream()
        except Exception:
            pass

        try:
            while not stop.is_set():
                for frame, repeats in pairs:
                    if stop.is_set():
                        break
                    with self._lock:
                        self._draw(adapter, capability, frame)
                    stop.wait(repeats * frame_ms / 1000.0)
                if len(pairs) == 1:
                    # A single still needs no loop; hold it and let the next
                    # publish replace it.
                    break
        except Exception:
            # The device dropped mid-animation. The next publish re-probes it;
            # the sink itself stays alive.
            pass
        finally:
            try:
                adapter.end_stream()
            except Exception:
                pass

    @staticmethod
    def _draw(adapter, capability: PanelCapability, frame: Frame) -> None:
        if capability.mode is PanelMode.PIXEL:
            adapter.draw_grid(_fit_to_matrix(frame, capability.matrix_size))
        elif capability.mode is PanelMode.SEGMENTS:
            adapter.set_segments(to_segments(frame, capability.segment_count))
        else:
            adapter.set_color(*average_color(frame))

    def _stop_playback(self) -> None:
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def close(self) -> None:
        self._stop_playback()
        adapter, self._adapter = self._adapter, None
        if adapter is None:
            return
        try:
            from ...drivers import pool

            if not pool.is_pooled(self.device):
                adapter.close()
        except Exception:
            pass


def _fit_to_matrix(frame: Frame, size: Optional[Tuple[int, int]]) -> List[List[RGB]]:
    """Resample a frame onto the panel's own grid.

    Nearest-neighbour, because the source is pixel art: a 52x32 dashboard shown
    on a 32x32 panel should drop columns, not blur them.
    """
    grid = frame.to_grid()
    if not size:
        return grid

    cols, rows = size
    if (cols, rows) == (frame.cols, frame.rows):
        return grid

    return [
        [grid[min(frame.rows - 1, y * frame.rows // rows)][min(frame.cols - 1, x * frame.cols // cols)]
         for x in range(cols)]
        for y in range(rows)
    ]
