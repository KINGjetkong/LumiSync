"""Hand-authored pixel art.

Each sprite is character rows plus a colour map, so the art is reviewable in a
diff and recolourable at draw time. Legend used throughout:

``.`` transparent  ``B`` body  ``A`` accent  ``E`` eye/highlight  ``S`` shadow
"""

from __future__ import annotations

from typing import Dict, Tuple

from .canvas import Sprite
from .palette import AMBER, BLACK, CYAN, GREEN, RED, VIOLET, WHITE, RGB, scale


def _sprite(rows: Tuple[str, ...], **colors) -> Sprite:
    return Sprite(rows=rows, colors={".": None, **colors})


# --- outcome badges -----------------------------------------------------

TROPHY = _sprite(
    (
        "B.BBBBB.B",
        "B.BBBBB.B",
        "B.BBBBB.B",
        ".BBBBBBB.",
        "..BBBBB..",
        "...BBB...",
        "...BBB...",
        "..BBBBB..",
        ".BBBBBBB.",
    ),
    B=AMBER,
)

SKULL = _sprite(
    (
        "..BBBBB..",
        ".BBBBBBB.",
        "BB.BBB.BB",
        "BB.BBB.BB",
        "BBBBBBBBB",
        ".B.BBB.B.",
        ".BBBBBBB.",
        "..B.B.B..",
    ),
    B=WHITE,
)

ARROW_UP = _sprite(
    (
        "..B..",
        ".BBB.",
        "BBBBB",
        "..B..",
        "..B..",
    ),
    B=GREEN,
)

ARROW_DOWN = _sprite(
    (
        "..B..",
        "..B..",
        "BBBBB",
        ".BBB.",
        "..B..",
    ),
    B=RED,
)

WARNING = _sprite(
    (
        "...B...",
        "..BBB..",
        "..BAB..",
        ".BBABB.",
        ".BBABB.",
        "BB.A.BB",
        "BB.A.BB",
        "BBBBBBB",
    ),
    B=AMBER,
    A=BLACK,
)

PLUG = _sprite(
    (
        ".B...B.",
        ".B...B.",
        "BBBBBBB",
        "BBBBBBB",
        "..BBB..",
        "..BBB..",
        "...B...",
    ),
    B=CYAN,
)


# --- agent avatars ------------------------------------------------------
#
# Four silhouettes so a row of agents reads as distinct individuals rather than
# a row of identical dots. Which one an agent gets is derived from its id, so it
# is stable for the lifetime of that agent.

AGENT_BOT = _sprite(
    (
        "...A...",
        ".BBBBB.",
        "BBBBBBB",
        "B.E.E.B",
        "BBBBBBB",
        "B.AAA.B",
        ".BBBBB.",
        "..B.B..",
    ),
    B=CYAN,
    A=WHITE,
    E=BLACK,
)

AGENT_ORB = _sprite(
    (
        "..AAA..",
        ".BBBBB.",
        "ABEBEBA",
        "BBBBBBB",
        "B.AAA.B",
        ".BBBBB.",
        "..AAA..",
    ),
    B=VIOLET,
    A=WHITE,
    E=BLACK,
)

AGENT_SCOPE = _sprite(
    (
        ".BBBBB.",
        "B.....B",
        "B.AAA.B",
        "B.AEA.B",
        "B.AAA.B",
        "B.....B",
        ".BBBBB.",
    ),
    B=GREEN,
    A=WHITE,
    E=BLACK,
)

AGENT_TOWER = _sprite(
    (
        "..AAA..",
        "..BBB..",
        ".BBBBB.",
        "BB.E.BB",
        "BBBBBBB",
        "BB.A.BB",
        "BBBBBBB",
        ".B...B.",
    ),
    B=AMBER,
    A=WHITE,
    E=BLACK,
)

AGENT_VARIANTS: Tuple[Sprite, ...] = (AGENT_BOT, AGENT_ORB, AGENT_SCOPE, AGENT_TOWER)

#: State -> body colour for an agent avatar. The silhouette says *who*, the
#: colour says *how it is doing*.
AGENT_STATE_COLORS: Dict[str, RGB] = {
    "idle": (92, 98, 116),
    "working": CYAN,
    "ok": GREEN,
    "warn": AMBER,
    "fail": RED,
}


def agent_sprite(agent_id: str, state: str) -> Sprite:
    """Pick a stable silhouette for an agent and tint it by state.

    The index comes from a checksum of the id rather than ``hash()``, which is
    salted per process and would give an agent a different face every restart.
    """
    checksum = sum((index + 1) * byte for index, byte in enumerate(str(agent_id).encode()))
    base = AGENT_VARIANTS[checksum % len(AGENT_VARIANTS)]
    body = AGENT_STATE_COLORS.get(str(state).lower(), AGENT_STATE_COLORS["idle"])
    return base.recolor({"B": body, "A": scale(body, 1.6) if state != "idle" else WHITE})


def outcome_sprite(good: bool) -> Sprite:
    """Trophy for a win, skull for a loss."""
    return TROPHY if good else SKULL
