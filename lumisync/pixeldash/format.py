"""Number and label formatting tuned for a panel three pixels per character wide.

The governing constraint: a figure has to be legible from across a room at
roughly 50 pixels of width. That rules out thousands separators and cents on
anything but small numbers, so values compact to ``K``/``M`` and the sign is
carried by both a glyph and a colour.

``None`` always formats as ``--``. That is the whole point — a missing number
must be visibly missing, not silently rendered as zero.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

MISSING = "--"


def money(value: Optional[float], *, signed: bool = True, symbol: bool = False) -> str:
    """Format a currency amount compactly.

    The currency symbol is off by default: a ``$`` is indistinguishable from an
    ``S`` in a three-pixel-wide font, and on a P&L panel the sign and the colour
    already say what the number is.

    >>> money(1240.0)
    '+1240'
    >>> money(-12345.0)
    '-12.3K'
    >>> money(None)
    '--'
    """
    if value is None:
        return MISSING

    magnitude = abs(value)
    if magnitude >= 1_000_000:
        body = f"{magnitude / 1_000_000:.1f}M"
    elif magnitude >= 10_000:
        body = f"{magnitude / 1_000:.1f}K"
    elif magnitude >= 1_000:
        body = f"{magnitude:.0f}"
    elif magnitude >= 10:
        body = f"{magnitude:.0f}"
    else:
        body = f"{magnitude:.2f}".rstrip("0").rstrip(".") or "0"

    prefix = ""
    if signed:
        prefix = "+" if value > 0 else "-" if value < 0 else ""
    elif value < 0:
        prefix = "-"
    return f"{prefix}{'$' if symbol else ''}{body}"


def percent(value: Optional[float], *, signed: bool = True, digits: int = 0) -> str:
    """Format a percentage that is already in percentage points."""
    if value is None:
        return MISSING
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.{digits}f}%"


def count(value: Optional[int]) -> str:
    return MISSING if value is None else str(int(value))


def price(value: Optional[float]) -> str:
    """Option premia need cents; equity prices do not."""
    if value is None:
        return MISSING
    if abs(value) < 100:
        return f"{value:.2f}"
    return f"{value:.0f}"


def short_symbol(symbol: str, underlying: str = "") -> str:
    """Squeeze an OCC option symbol into something readable on a panel.

    ``SPY260803C00550000`` becomes ``SPY550C``. Non-option symbols pass through.
    """
    from .feeds.base import parse_occ_symbol

    parsed = parse_occ_symbol(symbol)
    if not parsed:
        return (underlying or symbol or "").upper()[:8]

    root, _expiry, right, strike = parsed
    strike_text = f"{strike:.0f}" if float(strike).is_integer() else f"{strike:.1f}"
    return f"{root}{strike_text}{right}"


def clock(moment: Optional[_dt.datetime]) -> str:
    """24-hour ``HH:MM``, or ``--`` when the timestamp is missing."""
    return moment.strftime("%H:%M") if moment else MISSING


def day_label(date: _dt.date) -> str:
    return date.strftime("%d").lstrip("0") or "0"


def month_label(date: _dt.date) -> str:
    return date.strftime("%b").upper()


def fit_scale(text: str, max_width: int, *, preferred: int = 2, minimum: int = 1) -> int:
    """Largest font scale from ``preferred`` down to ``minimum`` that fits."""
    from .render import font

    for candidate in range(int(preferred), int(minimum) - 1, -1):
        if font.text_width(text, candidate) <= max_width:
            return candidate
    return max(1, int(minimum))
