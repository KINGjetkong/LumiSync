"""Pixel fonts, in two sizes, plus measurement helpers.

Two alphabets, chosen by how much room the target has:

``SMALL`` (3x5)
    The panel font. Three pixels wide is the smallest size at which digits stay
    unambiguous on a diffused LED panel, and it lets a 52-pixel row hold
    thirteen characters — enough for a symbol and its P&L.

``LARGE`` (5x7)
    For screen targets, where the dashboard is rendered on a bigger grid so
    each pixel is physically smaller. Five by seven has room for proper
    letterforms: real bowls on B/P/R, a true ``$``, and no letter that has to
    compromise.

Both are **variable width** — a glyph's width is the length of its rows. In the
3x5 font ``M``, ``N`` and ``W`` are 4-5 pixels because a three-pixel ``N``
either reads as an ``S`` (draw the diagonal) or as an ``M`` (fill the body), and
both mistakes land in words this dashboard shows constantly: OPEN, DOWN, WIN,
MISSION.

Glyphs are authored as strings so a correction is visible in the diff. ``#`` is
a lit pixel, ``.`` is transparent. Every row of a glyph must be the same length.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

Rows = Tuple[str, ...]
Offsets = Tuple[Tuple[int, int], ...]

FALLBACK = "?"
#: Blank columns between glyphs.
TRACKING = 1


# --------------------------------------------------------------- 3x5 alphabet

_SMALL: Dict[str, Rows] = {
    " ": ("...", "...", "...", "...", "..."),
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": (".##", "#..", "#..", "#..", ".##"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "F": ("###", "#..", "##.", "#..", "#.."),
    "G": (".##", "#..", "#.#", "#.#", ".##"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "J": ("..#", "..#", "..#", "#.#", ".#."),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    # The three wide letters. At 3px these collapse into each other; at 4-5px
    # the diagonal in N and the middle stems of M and W are unmistakable.
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#"),
    "N": ("#..#", "##.#", "#.##", "#..#", "#..#"),
    "O": ("###", "#.#", "#.#", "#.#", "###"),
    "P": ("##.", "#.#", "##.", "#..", "#.."),
    "Q": ("###", "#.#", "#.#", "###", "..#"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": (".##", "#..", ".#.", "..#", "##."),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"),
    "V": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "W": ("#...#", "#...#", "#.#.#", "##.##", "#...#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    ".": ("...", "...", "...", "...", ".#."),
    ",": ("...", "...", "...", ".#.", "#.."),
    ":": ("...", ".#.", "...", ".#.", "..."),
    ";": ("...", ".#.", "...", ".#.", "#.."),
    "-": ("...", "...", "###", "...", "..."),
    "+": ("...", ".#.", "###", ".#.", "..."),
    "=": ("...", "###", "...", "###", "..."),
    "_": ("...", "...", "...", "...", "###"),
    "/": ("..#", "..#", ".#.", "#..", "#.."),
    "\\": ("#..", "#..", ".#.", "..#", "..#"),
    "(": ("..#", ".#.", ".#.", ".#.", "..#"),
    ")": ("#..", ".#.", ".#.", ".#.", "#.."),
    "[": ("###", "#..", "#..", "#..", "###"),
    "]": ("###", "..#", "..#", "..#", "###"),
    "!": (".#.", ".#.", ".#.", "...", ".#."),
    "?": ("##.", "..#", ".#.", "...", ".#."),
    "*": ("#.#", ".#.", "###", ".#.", "#.#"),
    "#": ("#.#", "###", "#.#", "###", "#.#"),
    "%": ("#.#", "..#", ".#.", "#..", "#.#"),
    # A true '$' is not separable from 'S' at three pixels wide. This keeps the
    # centre stem so it reads as currency; the money formatter defaults to
    # omitting it entirely and letting the sign and colour carry the meaning.
    "$": (".#.", "###", "##.", ".##", "###"),
    "<": ("..#", ".#.", "#..", ".#.", "..#"),
    ">": ("#..", ".#.", "..#", ".#.", "#.."),
    "^": (".#.", "#.#", "...", "...", "..."),
    "'": (".#.", ".#.", "...", "...", "..."),
    '"': ("#.#", "#.#", "...", "...", "..."),
    "|": (".#.", ".#.", ".#.", ".#.", ".#."),
    "@": ("###", "#.#", "###", "#..", "###"),
    "~": ("...", "..#", "###", "#..", "..."),
    "·": ("...", "...", ".#.", "...", "..."),
}


# --------------------------------------------------------------- 5x7 alphabet

_LARGE: Dict[str, Rows] = {
    " ": ("...",) * 7,
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..##.", "....#", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": ("..#..", ".#.#.", "#...#", "#...#", "#####", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    ".": ("..", "..", "..", "..", "..", "##", "##"),
    ",": ("..", "..", "..", "..", "##", "##", "#."),
    ":": ("..", "##", "##", "..", "##", "##", ".."),
    ";": ("..", "##", "##", "..", "##", "##", "#."),
    "-": ("....", "....", "....", "####", "....", "....", "...."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    "\\": ("#....", "#....", ".#...", "..#..", "...#.", "....#", "....#"),
    "(": ("..#", ".#.", "#..", "#..", "#..", ".#.", "..#"),
    ")": ("#..", ".#.", "..#", "..#", "..#", ".#.", "#.."),
    "[": ("###", "#..", "#..", "#..", "#..", "#..", "###"),
    "]": ("###", "..#", "..#", "..#", "..#", "..#", "###"),
    "!": ("#", "#", "#", "#", "#", ".", "#"),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    "*": (".....", "#.#.#", ".###.", "#####", ".###.", "#.#.#", "....."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
    "%": ("##..#", "##..#", "...#.", "..#..", ".#...", "#..##", "#..##"),
    # At five pixels a real dollar sign fits: the S with a stem through it.
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "<": ("...#", "..#.", ".#..", "#...", ".#..", "..#.", "...#"),
    ">": ("#...", ".#..", "..#.", "...#", "..#.", ".#..", "#..."),
    "^": ("..#..", ".#.#.", "#...#", ".....", ".....", ".....", "....."),
    "'": ("#", "#", ".", ".", ".", ".", "."),
    '"': ("#.#", "#.#", "...", "...", "...", "...", "..."),
    "|": ("#", "#", "#", "#", "#", "#", "#"),
    "@": (".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".###."),
    "~": (".....", ".....", ".##.#", "#..##", ".....", ".....", "....."),
    "·": ("...", "...", "...", ".#.", "...", "...", "..."),
}


@dataclass(frozen=True)
class Font:
    """A pixel alphabet plus the measurements the layout needs."""

    name: str
    height: int
    glyphs: Dict[str, Offsets]
    widths: Dict[str, int]
    default_width: int

    # --- lookup ---
    def normalize(self, text: str) -> str:
        """Upper-case the text and replace anything this font cannot draw."""
        return "".join(
            char if char in self.glyphs else FALLBACK for char in str(text).upper()
        )

    def glyph(self, char: str) -> Offsets:
        return self.glyphs.get(char.upper(), self.glyphs[FALLBACK])

    def glyph_width(self, char: str) -> int:
        return self.widths.get(char.upper(), self.default_width)

    # --- measurement ---
    def text_width(self, text: str, scale: int = 1, tracking: int = TRACKING) -> int:
        """Pixel width of ``text``, excluding the trailing gap."""
        normalized = self.normalize(text)
        if not normalized:
            return 0
        glyphs = sum(self.glyph_width(char) for char in normalized)
        return glyphs * scale + (len(normalized) - 1) * tracking * scale

    def text_height(self, scale: int = 1) -> int:
        return self.height * scale

    def fit(self, text: str, max_width: int, scale: int = 1, tracking: int = TRACKING) -> str:
        """Truncate ``text`` so it fits ``max_width`` pixels.

        Truncation is silent and from the right — the composers write labels
        shortest-first, so anything that overflows is the least important part.
        """
        text = str(text)
        while text and self.text_width(text, scale, tracking) > max_width:
            text = text[:-1]
        return text

    def wrap(
        self,
        text: str,
        max_width: int,
        scale: int = 1,
        tracking: int = TRACKING,
        lines: int = 2,
    ) -> List[str]:
        """Break text on word boundaries into at most ``lines`` rows.

        Used for headlines like "MISSION FAILED" that do not fit one row.
        Wrapping beats squeezing the letter spacing: at this size, glyphs that
        touch stop being readable.
        """
        words = str(text).split()
        if not words:
            return [""]

        rows: List[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            too_wide = current and self.text_width(candidate, scale, tracking) > max_width
            if too_wide and len(rows) < max(1, lines) - 1:
                rows.append(current)
                current = word
            else:
                # On the last allowed row, keep appending and let ``fit`` truncate.
                current = candidate
        rows.append(current)
        return [self.fit(row, max_width, scale, tracking) for row in rows]


def build_font(name: str, raw: Dict[str, Rows]) -> Font:
    """Compile authored glyph strings into lit-pixel offsets.

    Offsets are precomputed once; the blitter walks them rather than re-parsing
    strings on every frame. Ragged glyphs are a hard error — a glyph whose rows
    disagree on width would silently mis-measure every string containing it.
    """
    glyphs: Dict[str, Offsets] = {}
    widths: Dict[str, int] = {}
    heights = set()

    for char, rows in raw.items():
        row_widths = {len(row) for row in rows}
        if len(row_widths) != 1:
            raise ValueError(f"{name}: glyph {char!r} has ragged rows")
        widths[char] = row_widths.pop()
        heights.add(len(rows))
        glyphs[char] = tuple(
            (x, y)
            for y, row in enumerate(rows)
            for x, cell in enumerate(row)
            if cell == "#"
        )

    if len(heights) != 1:
        raise ValueError(f"{name}: glyphs disagree on height: {sorted(heights)}")

    return Font(
        name=name,
        height=heights.pop(),
        glyphs=glyphs,
        widths=widths,
        default_width=widths.get(FALLBACK, 3),
    )


SMALL = build_font("3x5", _SMALL)
LARGE = build_font("5x7", _LARGE)

#: Fonts in ascending size, for pickers that want the biggest that fits.
FONTS = (SMALL, LARGE)

#: The panel font remains the default everywhere a font is not passed
#: explicitly, so existing call sites keep their behaviour.
DEFAULT = SMALL

# Backwards-compatible module-level surface over the small font.
GLYPH_WIDTH = 3
GLYPH_HEIGHT = SMALL.height
GLYPHS = SMALL.glyphs
WIDTHS = SMALL.widths
_RAW = _SMALL


def normalize(text: str) -> str:
    return SMALL.normalize(text)


def glyph(char: str) -> Offsets:
    return SMALL.glyph(char)


def glyph_width(char: str) -> int:
    return SMALL.glyph_width(char)


def text_width(text: str, scale: int = 1, tracking: int = TRACKING) -> int:
    return SMALL.text_width(text, scale, tracking)


def text_height(scale: int = 1) -> int:
    return SMALL.text_height(scale)


def fit(text: str, max_width: int, scale: int = 1, tracking: int = TRACKING) -> str:
    return SMALL.fit(text, max_width, scale, tracking)


def wrap(
    text: str, max_width: int, scale: int = 1, tracking: int = TRACKING, lines: int = 2
) -> List[str]:
    return SMALL.wrap(text, max_width, scale, tracking, lines)
