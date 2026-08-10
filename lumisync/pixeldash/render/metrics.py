"""Layout measurements derived from the render target.

The scene composers used to carry hard-coded pixel constants — header at 6,
body at 8, rows every 8. That works for exactly one panel size. This module
turns those into a small set of measurements computed from the target, so the
same scenes lay out correctly on a 16x16 panel, on the 52x32 H6631, and on a
156x96 screen surface where each pixel is physically much smaller.

The only real decision in here is which font a target gets, and it is made on
available rows rather than columns: a font is chosen by whether a header, a
body and a footer line all fit vertically. A wide-but-short target that took
the 5x7 face on width alone would have room for the letters and nowhere to put
them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import RenderTarget
from . import font

#: How many position rows a page shows. Metrics needs it to spread the rows
#: across the body height; :mod:`scenes` imports it from here so the two can
#: never drift apart.
POSITION_ROWS = 3

#: The widest value the hero number takes in its common band — anything up to
#: a six-figure day. Sizing against a specimen rather than the current value
#: keeps the number from resizing as the day moves. A rarer, wider value still
#: renders correctly: ``scene_daily`` runs ``fit_scale`` per value and drops a
#: step for it, so this only has to be the size worth optimising the layout for.
HERO_SPECIMEN = "-99.9K"
MAX_HERO_SCALE = 3


@dataclass(frozen=True)
class Metrics:
    """Everything a composer needs to place things on this target."""

    target: RenderTarget
    face: font.Font
    margin: int
    header_height: int      # baseline-to-rule, excluding the rule itself
    body_top: int           # first usable body row
    line_height: int        # one text row plus its gap
    row_pitch: int          # a positions row plus its bar and gap
    cell: int               # calendar cell size
    cell_pitch: int         # calendar cell plus gap
    avatar_pitch: int       # agent avatar plus gap
    hero_scale: int         # scale for the big P&L number
    compact: bool           # too small for a header and a body

    # --- derived ---
    @property
    def cols(self) -> int:
        return self.target.cols

    @property
    def rows(self) -> int:
        return self.target.rows

    @property
    def left(self) -> int:
        return self.margin

    @property
    def right(self) -> int:
        """Exclusive right edge of the content area."""
        return self.target.cols - self.margin

    @property
    def content_width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def rule_y(self) -> int:
        return self.header_height

    def line(self, index: int) -> int:
        """Top row of the ``index``-th body text line."""
        return self.body_top + index * self.line_height

    def lines_available(self, from_y: int = -1) -> int:
        """How many text lines still fit below ``from_y``."""
        start = self.body_top if from_y < 0 else from_y
        return max(0, (self.rows - start) // self.line_height)

    def fits(self, y: int, height: int) -> bool:
        return y >= 0 and y + height <= self.rows


def pick_face(target: RenderTarget) -> font.Font:
    """Largest font whose header, body and footer all fit this target.

    Requires the 5x7 face to have room for a header line, a rule, and at least
    three body lines — the minimum any scene here uses — plus the width to show
    a realistic label at that size.
    """
    for face in reversed(font.FONTS):
        line_height = face.height + 2
        needed_rows = line_height * 4 + 2
        needed_cols = face.text_width("MISSION", 1) + 4
        if target.rows >= needed_rows and target.cols >= needed_cols:
            return face
    return font.SMALL


def metrics_for(target: RenderTarget) -> Metrics:
    """Compute the layout for a target."""
    face = pick_face(target)
    compact = target.cols < 32 or target.rows < 24

    margin = 1 if face is font.SMALL else 2
    line_height = face.height + 2
    header_height = face.height + 1
    body_top = header_height + 2

    # A positions row is a label plus a bar underneath, so its floor is a line
    # and change. Above that floor the rows spread to fill the body: on a tall
    # target, three rows crammed into the top third with dead space beneath
    # reads as a rendering bug rather than as a layout.
    body_height = max(1, target.rows - body_top)
    row_floor = face.height + 3  # label, bar, gap
    row_pitch = max(row_floor, body_height // POSITION_ROWS)

    # Calendar cells scale with the panel so seven weekday rows always fit the
    # body, with a one-pixel gutter between them.
    cell_pitch = max(2, min(body_height // 7, max(3, target.cols // 16)))
    cell = max(1, cell_pitch - 1)

    avatar_pitch = 9 if face is font.SMALL else 14

    # The hero number takes the largest multiple of the face that still leaves
    # room for the record line underneath. Only one line is reserved: on a
    # 52x32 panel read from across the room, the size of today's P&L matters
    # more than the session breakdown, and the lines below it are drawn only
    # when they genuinely fit. Measured against the widest realistic value, not
    # the current one, so the number does not resize as the day moves.
    hero_scale = 1
    for candidate in range(MAX_HERO_SCALE, 0, -1):
        wide_enough = face.text_width(HERO_SPECIMEN, candidate) <= target.cols - margin * 2
        tall_enough = body_top + face.height * candidate + 3 + line_height <= target.rows
        if wide_enough and tall_enough:
            hero_scale = candidate
            break

    return Metrics(
        target=target,
        face=face,
        margin=margin,
        header_height=header_height,
        body_top=body_top,
        line_height=line_height,
        row_pitch=row_pitch,
        cell=cell,
        cell_pitch=cell_pitch,
        avatar_pitch=avatar_pitch,
        hero_scale=hero_scale,
        compact=compact,
    )
