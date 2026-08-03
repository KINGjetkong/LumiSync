"""A mutable RGB pixel buffer with just enough drawing primitives.

Backed by a numpy array because the sync engine already depends on numpy and
because converting to the driver's row-major grid then costs one ``tolist()``.
Drawing is clipped, never wrapped: a label that runs off the right edge is cut,
which is always better on a 52-pixel panel than wrapping into the next row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import font
from .palette import BLACK, RGB


@dataclass(frozen=True)
class Sprite:
    """Pixel art authored as character rows plus a colour map.

    A ``None`` entry in ``colors`` means transparent, so sprites compose over
    whatever is already on the frame.
    """

    rows: Tuple[str, ...]
    colors: dict

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def height(self) -> int:
        return len(self.rows)

    def recolor(self, mapping: dict) -> "Sprite":
        """Return a copy with some or all colour slots replaced."""
        return Sprite(rows=self.rows, colors={**self.colors, **mapping})


class Frame:
    """One panel-sized image."""

    __slots__ = ("cols", "rows", "data")

    def __init__(self, cols: int, rows: int, background: RGB = BLACK) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        self.data = np.zeros((self.rows, self.cols, 3), dtype=np.uint8)
        if background != (0, 0, 0):
            self.data[:, :] = background

    # --- lifecycle ---
    def copy(self) -> "Frame":
        clone = Frame(self.cols, self.rows)
        clone.data = self.data.copy()
        return clone

    def clear(self, color: RGB = BLACK) -> "Frame":
        self.data[:, :] = color
        return self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return (
            self.cols == other.cols
            and self.rows == other.rows
            and np.array_equal(self.data, other.data)
        )

    def __hash__(self) -> int:
        return hash((self.cols, self.rows, self.data.tobytes()))

    # --- primitives ---
    def set_pixel(self, x: int, y: int, color: RGB) -> None:
        if 0 <= x < self.cols and 0 <= y < self.rows:
            self.data[y, x] = color

    def get_pixel(self, x: int, y: int) -> RGB:
        return tuple(int(channel) for channel in self.data[y, x])

    def rect(self, x: int, y: int, width: int, height: int, color: RGB) -> None:
        """Filled rectangle, clipped to the frame."""
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.cols, x + width), min(self.rows, y + height)
        if x1 > x0 and y1 > y0:
            self.data[y0:y1, x0:x1] = color

    def outline(self, x: int, y: int, width: int, height: int, color: RGB) -> None:
        self.hline(x, y, width, color)
        self.hline(x, y + height - 1, width, color)
        self.vline(x, y, height, color)
        self.vline(x + width - 1, y, height, color)

    def hline(self, x: int, y: int, width: int, color: RGB) -> None:
        self.rect(x, y, width, 1, color)

    def vline(self, x: int, y: int, height: int, color: RGB) -> None:
        self.rect(x, y, 1, height, color)

    def dim(self, factor: float) -> None:
        """Scale every channel — used for the fade in notification bursts."""
        scaled = self.data.astype(np.float32) * max(0.0, float(factor))
        self.data = np.clip(scaled, 0, 255).astype(np.uint8)

    # --- text ---
    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: RGB,
        *,
        scale: int = 1,
        tracking: int = 1,
    ) -> int:
        """Draw ``value`` with its top-left at ``(x, y)``; return the width drawn."""
        scale = max(1, int(scale))
        cursor = x
        for char in font.normalize(value):
            for offset_x, offset_y in font.glyph(char):
                self.rect(
                    cursor + offset_x * scale,
                    y + offset_y * scale,
                    scale,
                    scale,
                    color,
                )
            cursor += (font.glyph_width(char) + tracking) * scale
        return max(0, cursor - x - tracking * scale)

    def text_centered(
        self,
        y: int,
        value: str,
        color: RGB,
        *,
        scale: int = 1,
        tracking: int = 1,
        left: int = 0,
        right: Optional[int] = None,
    ) -> int:
        """Centre text horizontally inside ``[left, right)``."""
        right = self.cols if right is None else right
        span = max(0, right - left)
        # Tighten the letter spacing before truncating: losing the last letters
        # of a headline is a worse outcome than losing a pixel of air between
        # them.
        if font.text_width(value, scale, tracking) > span and tracking > 0:
            tracking = 0
        value = font.fit(value, span, scale, tracking)
        width = font.text_width(value, scale, tracking)
        return self.text(left + (span - width) // 2, y, value, color, scale=scale, tracking=tracking)

    def text_right(
        self,
        right: int,
        y: int,
        value: str,
        color: RGB,
        *,
        scale: int = 1,
        tracking: int = 1,
    ) -> int:
        width = font.text_width(str(value), scale, tracking)
        return self.text(right - width, y, value, color, scale=scale, tracking=tracking)

    # --- sprites ---
    def blit(self, sprite: Sprite, x: int, y: int, *, scale: int = 1) -> None:
        scale = max(1, int(scale))
        for row_index, row in enumerate(sprite.rows):
            for col_index, char in enumerate(row):
                color = sprite.colors.get(char)
                if color is None:
                    continue
                self.rect(
                    x + col_index * scale,
                    y + row_index * scale,
                    scale,
                    scale,
                    color,
                )

    def blit_frame(self, other: "Frame", x: int, y: int) -> None:
        """Composite another frame at an offset, clipped to this one."""
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.cols, x + other.cols), min(self.rows, y + other.rows)
        if x1 <= x0 or y1 <= y0:
            return
        self.data[y0:y1, x0:x1] = other.data[y0 - y : y1 - y, x0 - x : x1 - x]

    # --- export ---
    def to_grid(self) -> List[List[Tuple[int, int, int]]]:
        """Row-major list of RGB tuples, the shape matrix drivers expect."""
        return [[tuple(int(c) for c in pixel) for pixel in row] for row in self.data]

    def to_flat(self) -> List[Tuple[int, int, int]]:
        """Flattened row-major pixels, for per-segment device streams."""
        return [tuple(int(c) for c in pixel) for row in self.data for pixel in row]

    def tobytes(self) -> bytes:
        return self.data.tobytes()

    def upscale(self, factor: int) -> np.ndarray:
        """Nearest-neighbour enlargement — pixels must stay square and hard."""
        factor = max(1, int(factor))
        return np.repeat(np.repeat(self.data, factor, axis=0), factor, axis=1)


def frames_equal(left: Sequence[Frame], right: Sequence[Frame]) -> bool:
    return len(left) == len(right) and all(a == b for a, b in zip(left, right))


def hold(frame: Frame, count: int) -> List[Frame]:
    """Repeat a frame ``count`` times — how a static scene becomes a duration."""
    return [frame.copy() for _ in range(max(1, int(count)))]


def concat(*groups: Iterable[Frame]) -> List[Frame]:
    result: List[Frame] = []
    for group in groups:
        result.extend(group)
    return result
