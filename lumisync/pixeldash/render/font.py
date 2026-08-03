"""A mostly-3x5 pixel font, plus measurement helpers.

Three pixels wide is the smallest size at which digits stay unambiguous on a
diffused LED panel, and it lets a 52-pixel row hold thirteen characters — enough
for a symbol and its P&L. Headlines use the same glyphs at 2x scale so there is
only ever one alphabet to maintain.

Three letters get more room. ``M``, ``N`` and ``W`` are the letters a
three-pixel grid genuinely cannot express: every 3x5 ``N`` either reads as an
``S`` (if you draw the diagonal) or as an ``M`` (if you fill the body), and both
mistakes land in words this dashboard shows constantly — OPEN, DOWN, WIN,
MISSION. So the font is variable-width: those three are 4-5 pixels, everything
else stays at 3.

Glyphs are authored as strings so a correction is visible in the diff. ``#`` is
a lit pixel, ``.`` is transparent. Every row of a glyph must be the same length;
that length is the glyph's width.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

#: Width of the common case. Individual glyphs may be wider — always ask
#: :func:`glyph_width` rather than assuming this.
GLYPH_WIDTH = 3
GLYPH_HEIGHT = 5
#: Blank columns between glyphs.
TRACKING = 1

_RAW: Dict[str, Tuple[str, str, str, str, str]] = {
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
    "·": ("...", "...", ".#.", "...", "..."),  # middle dot
}

#: Character -> list of (x, y) lit offsets. Precomputed once; the blitter walks
#: these rather than re-parsing strings for every frame.
GLYPHS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    char: tuple(
        (x, y)
        for y, row in enumerate(rows)
        for x, cell in enumerate(row)
        if cell == "#"
    )
    for char, rows in _RAW.items()
}

#: Character -> advance width in pixels.
WIDTHS: Dict[str, int] = {
    char: max(len(row) for row in rows) for char, rows in _RAW.items()
}

FALLBACK = "?"


def normalize(text: str) -> str:
    """Upper-case the text and replace anything the font cannot draw."""
    result: List[str] = []
    for char in str(text).upper():
        result.append(char if char in GLYPHS else FALLBACK)
    return "".join(result)


def glyph(char: str) -> Tuple[Tuple[int, int], ...]:
    return GLYPHS.get(char.upper(), GLYPHS[FALLBACK])


def glyph_width(char: str) -> int:
    return WIDTHS.get(char.upper(), GLYPH_WIDTH)


def text_width(text: str, scale: int = 1, tracking: int = 1) -> int:
    """Pixel width of ``text``, excluding the trailing gap."""
    normalized = normalize(text)
    if not normalized:
        return 0
    glyphs = sum(glyph_width(char) for char in normalized)
    return glyphs * scale + (len(normalized) - 1) * tracking * scale


def text_height(scale: int = 1) -> int:
    return GLYPH_HEIGHT * scale


def wrap(text: str, max_width: int, scale: int = 1, tracking: int = 1, lines: int = 2) -> List[str]:
    """Break text on word boundaries into at most ``lines`` rows.

    Used for headlines like "MISSION FAILED" that do not fit one 52-pixel row.
    Wrapping beats squeezing the letter spacing: at this size, glyphs that touch
    stop being readable.
    """
    words = str(text).split()
    if not words:
        return [""]

    rows: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        too_wide = current and text_width(candidate, scale, tracking) > max_width
        if too_wide and len(rows) < max(1, lines) - 1:
            rows.append(current)
            current = word
        else:
            # On the last allowed row, keep appending and let ``fit`` truncate.
            current = candidate
    rows.append(current)
    return [fit(row, max_width, scale, tracking) for row in rows]


def fit(text: str, max_width: int, scale: int = 1, tracking: int = 1) -> str:
    """Truncate ``text`` so it fits ``max_width`` pixels.

    Truncation is silent and from the right — labels are written shortest-first
    by the composers, so anything that overflows is already the least important
    part of the string.
    """
    text = str(text)
    while text and text_width(text, scale, tracking) > max_width:
        text = text[:-1]
    return text
