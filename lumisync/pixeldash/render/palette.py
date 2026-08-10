"""Colour vocabulary for the panel.

An LED matrix behind a diffuser is not a monitor: mid-tones wash out, pure blue
reads as dim, and two colours that are distinct on screen can be identical from
across the room. These values are chosen for that medium — high chroma, wide
separation, and a green/red pair that stays distinguishable to the most common
form of colour blindness by pairing hue with brightness rather than hue alone.
"""

from __future__ import annotations

from typing import Dict, Tuple

RGB = Tuple[int, int, int]

BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)
DIM: RGB = (58, 62, 74)
GREY: RGB = (128, 134, 150)

#: Profit / loss. Green is brighter than red on purpose: at a glance the
#: brightness carries the sign even when the hue does not.
GREEN: RGB = (46, 230, 118)
RED: RGB = (255, 62, 72)
AMBER: RGB = (255, 176, 32)
CYAN: RGB = (56, 214, 255)
VIOLET: RGB = (172, 108, 255)
BLUE: RGB = (64, 132, 255)

#: Semantic roles used by the scene composers.
ROLES: Dict[str, RGB] = {
    "bg": BLACK,
    "text": WHITE,
    "muted": GREY,
    "dim": DIM,
    "good": GREEN,
    "bad": RED,
    "warn": AMBER,
    "info": CYAN,
    "accent": VIOLET,
    "live": RED,
    "paper": CYAN,
}

#: Heat ramps for the journal calendar, index 0 (flat) through 4 (max).
GREEN_RAMP: Tuple[RGB, ...] = (
    (22, 30, 28),
    (16, 84, 56),
    (24, 140, 84),
    (34, 190, 108),
    GREEN,
)
RED_RAMP: Tuple[RGB, ...] = (
    (32, 22, 24),
    (96, 26, 32),
    (152, 34, 42),
    (206, 46, 54),
    RED,
)
#: A traded-but-flat day still needs to be visibly *traded*.
FLAT: RGB = (70, 74, 88)
#: A day with no trades at all.
UNTRADED: RGB = (14, 15, 20)


def heat_color(level: int) -> RGB:
    """Colour for a signed heat index from :func:`~lumisync.pixeldash.stats.heat_level`."""
    magnitude = min(abs(int(level)), len(GREEN_RAMP) - 1)
    if level > 0:
        return GREEN_RAMP[magnitude]
    if level < 0:
        return RED_RAMP[magnitude]
    return FLAT


def pnl_color(value: float) -> RGB:
    """Green above zero, red below, neutral at exactly flat."""
    if value > 0:
        return GREEN
    if value < 0:
        return RED
    return GREY


def role(name: str, default: RGB = WHITE) -> RGB:
    return ROLES.get(name, default)


def scale(color: RGB, factor: float) -> RGB:
    """Multiply a colour's brightness, clamped to the 8-bit range."""
    factor = max(0.0, factor)
    return tuple(min(255, max(0, int(round(channel * factor)))) for channel in color)


def mix(start: RGB, end: RGB, amount: float) -> RGB:
    """Linear blend, ``amount`` 0 -> start, 1 -> end."""
    amount = min(1.0, max(0.0, amount))
    return tuple(
        int(round(a + (b - a) * amount)) for a, b in zip(start, end)
    )
