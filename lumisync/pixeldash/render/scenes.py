"""Scene composers — snapshot in, frames out.

Every composer is a pure function of its :class:`SceneContext`. Nothing here
reads a clock, opens a socket, or calls ``random`` directly; the only source of
variation is ``context.rng``, which the pipeline seeds from the snapshot digest.
That is what makes a render reproducible.

Layout is written against ``context.target`` rather than hard-coded to 52x32, so
the same scenes lay out on a 32x32 or 64x32 panel. Below 32 columns the
composers switch to a stripped-down variant — a 16x16 panel can show a number
and a colour, and pretending otherwise just produces mush.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .. import format as fmt
from ..config import PixelDashConfig
from ..events import DashEvent, EventKind
from ..models import DashboardSnapshot, DataClass, Position, RenderTarget
from ..stats import Summary, heat_level, heat_scale, recent_grid, summarize
from . import sprites
from .canvas import Frame, hold
from .palette import (
    BLACK,
    CYAN,
    GREY,
    RED,
    UNTRADED,
    WHITE,
    RGB,
    heat_color,
    mix,
    pnl_color,
    role,
    scale as scale_color,
)
from .planner import ScenePlan

HEADER_HEIGHT = 6
BODY_TOP = 8


@dataclass(frozen=True)
class SceneContext:
    """Everything a composer is allowed to see."""

    snapshot: DashboardSnapshot
    target: RenderTarget
    plan: ScenePlan
    config: PixelDashConfig
    summary: Summary
    rng: random.Random

    @property
    def cols(self) -> int:
        return self.target.cols

    @property
    def rows(self) -> int:
        return self.target.rows

    @property
    def compact(self) -> bool:
        """True on panels too small for a header and a body."""
        return self.target.cols < 32 or self.target.rows < 24

    def blank(self) -> Frame:
        return Frame(self.cols, self.rows, BLACK)

    def frames_for(self, seconds: float) -> int:
        """How many frames fill ``seconds`` at the configured frame rate."""
        per_frame = max(1, int(self.config.frame_ms))
        return max(1, int(round(seconds * 1000.0 / per_frame)))


def build_context(
    snapshot: DashboardSnapshot,
    config: PixelDashConfig,
    plan: ScenePlan,
    rng: random.Random,
) -> SceneContext:
    return SceneContext(
        snapshot=snapshot,
        target=config.target,
        plan=plan,
        config=config,
        summary=summarize(snapshot),
        rng=rng,
    )


# --- shared chrome ------------------------------------------------------

def chrome(
    frame: Frame,
    context: SceneContext,
    title: str,
    *,
    right_text: Optional[str] = None,
    right_color: Optional[RGB] = None,
) -> None:
    """Draw the title row and the rule beneath it.

    ``right_text`` defaults to the data-class chip — live and paper money must
    never be mistaken for each other from across the room. Passing a string
    replaces the chip; passing ``""`` suppresses it, which the setup card uses
    because there is no account to classify yet.
    """
    if context.compact:
        return

    if right_text is None:
        right_text = context.snapshot.data_class.short
        right_color = RED if context.snapshot.data_class is DataClass.LIVE else CYAN
    right_color = right_color or GREY

    from . import font

    right_width = font.text_width(right_text) if right_text else 0
    title_space = context.cols - right_width - 3
    frame.text(1, 0, font.fit(title, title_space), context.plan.accent)
    if right_text:
        frame.text_right(context.cols - 1, 0, right_text, right_color)
    frame.hline(0, HEADER_HEIGHT, context.cols, scale_color(context.plan.accent, 0.30))


def marquee(text: str, width: int, offset: int) -> str:
    """Window into a horizontally scrolling string.

    Returns ``text`` unchanged when it already fits, so short labels never
    wobble.
    """
    from . import font

    if font.text_width(text) <= width:
        return text
    padded = f"{text}   "
    start = offset % len(padded)
    rotated = padded[start:] + padded[:start]
    return font.fit(rotated, width)


# --- daily --------------------------------------------------------------

def scene_daily(context: SceneContext) -> List[Frame]:
    """The headline card: today's realized P&L, record and session split."""
    summary = context.summary
    frame = context.blank()

    realized = summary.realized_today
    color = pnl_color(realized) if realized is not None else GREY
    text = fmt.money(realized)

    if context.compact:
        size = fmt.fit_scale(text, context.cols - 2, preferred=2)
        frame.text_centered((context.rows - 5 * size) // 2, text, color, scale=size)
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "TODAY")

    size = fmt.fit_scale(text, context.cols - 4, preferred=2)
    frame.text_centered(BODY_TOP + 1, text, color, scale=size)

    record_y = BODY_TOP + 1 + 5 * size + 3
    if summary.trades_today:
        record = f"{summary.wins_today}W {summary.losses_today}L"
        frame.text(1, record_y, record, WHITE)
        rate = fmt.percent(summary.win_rate_today, signed=False)
        frame.text_right(context.cols - 1, record_y, rate, _rate_color(summary.win_rate_today))
    else:
        # No closed trades yet is a real state, and it is not "flat".
        frame.text(1, record_y, "NO CLOSES YET", GREY)

    session_y = record_y + 7
    if session_y + 5 <= context.rows:
        _session_split(frame, context, session_y)

    return hold(frame, context.frames_for(context.config.scene_seconds))


def _rate_color(rate: Optional[float]) -> RGB:
    if rate is None:
        return GREY
    if rate >= 60:
        return role("good")
    if rate >= 45:
        return role("warn")
    return role("bad")


def _session_split(frame: Frame, context: SceneContext, y: int) -> None:
    """RTH vs ETH realized — the two behave differently enough to separate."""
    summary = context.summary
    frame.text(1, y, "R", GREY)
    frame.text(5, y, fmt.money(summary.rth_today), pnl_color(summary.rth_today))

    middle = context.cols // 2 + 2
    frame.text(middle, y, "E", GREY)
    frame.text(
        middle + 4, y, fmt.money(summary.eth_today), pnl_color(summary.eth_today)
    )


# --- positions ----------------------------------------------------------

ROW_PITCH = 8
ROWS_PER_PAGE = 3


def scene_positions(context: SceneContext) -> List[Frame]:
    """Open risk, biggest first, paged three at a time."""
    positions = list(context.snapshot.positions)
    if not positions:
        return _empty_card(context, "OPEN", "NO POSITIONS")

    rows_available = max(1, (context.rows - BODY_TOP) // ROW_PITCH)
    per_page = min(ROWS_PER_PAGE, rows_available)
    pages = [positions[i : i + per_page] for i in range(0, len(positions), per_page)]
    seconds_per_page = context.config.scene_seconds / len(pages)

    frames: List[Frame] = []
    for page in pages:
        frame = context.blank()
        unrealized = context.snapshot.open_unrealized
        chrome(
            frame,
            context,
            f"OPEN {len(positions)}",
            # With an unmarked leg the total is refused, so fall back to the
            # data-class chip rather than showing a misleading partial sum.
            right_text=fmt.money(unrealized) if unrealized is not None else None,
            right_color=pnl_color(unrealized) if unrealized is not None else None,
        )
        for index, position in enumerate(page):
            _position_row(frame, context, position, BODY_TOP + index * ROW_PITCH)
        frames.extend(hold(frame, context.frames_for(seconds_per_page)))
    return frames


def _position_row(frame: Frame, context: SceneContext, position: Position, y: int) -> None:
    from . import font

    label = fmt.short_symbol(position.symbol, position.underlying)
    change = position.unrealized_pct
    right = fmt.percent(change, digits=0) if change is not None else fmt.MISSING
    color = pnl_color(change) if change is not None else GREY

    right_width = font.text_width(right)
    frame.text(1, y, font.fit(label, context.cols - right_width - 4), WHITE)
    frame.text_right(context.cols - 1, y, right, color)

    # A proportional bar makes three positions comparable at a glance without
    # asking anyone to read three numbers.
    bar_y = y + 6
    if bar_y < context.rows and change is not None:
        span = context.cols - 2
        filled = int(round(min(1.0, abs(change) / 100.0) * span))
        frame.hline(1, bar_y, span, UNTRADED)
        if filled:
            frame.hline(1, bar_y, filled, scale_color(color, 0.85))


# --- calendar -----------------------------------------------------------

CELL = 2
CELL_PITCH = 3


def scene_calendar(context: SceneContext) -> List[Frame]:
    """The journal heat grid: seven rows of weekdays, weeks running across.

    Colour encodes the day's realized P&L against the window's biggest day, so
    the grid self-scales to the account. Untraded days stay near-black — an
    unlit square means "no trades", never "flat".
    """
    days = list(context.snapshot.days)
    if not days:
        return _empty_card(context, "JRNL", "NO HISTORY")

    frame = context.blank()
    window_pnl = context.summary.window_realized
    chrome(
        frame,
        context,
        "JRNL",
        right_text=fmt.money(window_pnl),
        right_color=pnl_color(window_pnl),
    )

    top = BODY_TOP + 1
    # As many weeks as fit across, capped at a quarter — beyond that the
    # columns are too thin to read and the window is longer than the trader's
    # working memory anyway.
    weeks = max(1, min((context.cols - 2) // CELL_PITCH, 16))

    end = context.snapshot.captured_at.date()
    grid = recent_grid(days, end, weeks=weeks)
    scale_value = heat_scale(days)

    for column, week in enumerate(grid):
        for row, cell in enumerate(week):
            x = 1 + column * CELL_PITCH
            y = top + row * CELL_PITCH
            if y + CELL > context.rows:
                continue
            if cell.stats is None or cell.stats.trades == 0:
                frame.rect(x, y, CELL, CELL, UNTRADED if cell.in_month else BLACK)
                continue
            color = heat_color(heat_level(cell.stats.realized, scale_value))
            frame.rect(x, y, CELL, CELL, color)
            if cell.date == end:
                # Ring today so the eye finds "now" in the grid immediately.
                frame.outline(x - 1, y - 1, CELL + 2, CELL + 2, WHITE)

    return hold(frame, context.frames_for(context.config.scene_seconds))


# --- agents -------------------------------------------------------------

AVATAR_PITCH = 9


def scene_agents(context: SceneContext) -> List[Frame]:
    """A row of agent avatars, focus stepping through them.

    This is the "something moved in the workflow" surface: each agent's body
    colour is its state, so a red silhouette in the row is visible long before
    anyone reads the label.
    """
    agents = list(context.snapshot.agents)
    if not agents:
        return _empty_card(context, "AGENTS", "NONE")

    visible = agents[: max(1, (context.cols - 2) // AVATAR_PITCH)]
    seconds_each = context.config.scene_seconds / len(visible)
    frames: List[Frame] = []

    for focus_index, focused in enumerate(visible):
        frame = context.blank()
        chrome(frame, context, "AGENTS")

        for index, agent in enumerate(visible):
            x = 1 + index * AVATAR_PITCH
            sprite = sprites.agent_sprite(agent.agent_id, agent.state)
            frame.blit(sprite, x, BODY_TOP)
            if index == focus_index:
                marker_y = BODY_TOP + sprite.height + 1
                if marker_y < context.rows:
                    frame.hline(x, marker_y, sprite.width, context.plan.accent)

        label_y = BODY_TOP + 11
        if label_y + 5 <= context.rows:
            frame.text(1, label_y, focused.label, WHITE)
            state_color = sprites.AGENT_STATE_COLORS.get(focused.state.lower(), GREY)
            frame.text_right(context.cols - 1, label_y, focused.state.upper()[:4], state_color)

        message_y = label_y + 7
        if message_y + 5 <= context.rows and focused.message:
            steps = context.frames_for(seconds_each)
            for step in range(steps):
                scrolled = frame.copy()
                scrolled.text(
                    1,
                    message_y,
                    marquee(focused.message.upper(), context.cols - 2, step),
                    GREY,
                )
                frames.append(scrolled)
            continue

        frames.extend(hold(frame, context.frames_for(seconds_each)))
    return frames


# --- failure states -----------------------------------------------------

def scene_error(context: SceneContext) -> List[Frame]:
    """Draw the broken feeds. This card exists so a dead feed is never silent."""
    failed = context.snapshot.failed_feeds
    frame = context.blank()

    if context.compact:
        frame.rect(0, 0, context.cols, context.rows, (40, 0, 0))
        frame.text_centered(context.rows // 2 - 3, "FEED", RED)
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "FEED DOWN", right_text="!", right_color=RED)
    frame.blit(sprites.WARNING, 2, BODY_TOP + 2)

    left = 2 + sprites.WARNING.width + 3
    names = ", ".join(status.name.upper() for status in failed) or "UNKNOWN"
    frame.text(left, BODY_TOP + 2, names[: max(1, (context.cols - left) // 4)], RED)

    detail = failed[0].detail if failed else "no detail"
    steps = context.frames_for(context.config.scene_seconds)
    frames: List[Frame] = []
    for step in range(steps):
        scrolled = frame.copy()
        scrolled.text(1, context.rows - 6, marquee(detail.upper(), context.cols - 2, step), GREY)
        frames.append(scrolled)
    return frames


def scene_setup(context: SceneContext) -> List[Frame]:
    """Shown when no broker is configured — the only card with no numbers on it."""
    from ..config import setup_instructions

    frame = context.blank()
    if context.compact:
        frame.text_centered(context.rows // 2 - 3, "SET UP", CYAN)
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "SETUP", right_text="")
    frame.blit(sprites.PLUG, 2, BODY_TOP + 2)
    frame.text(12, BODY_TOP + 2, "NO BROKER", CYAN)

    notes = setup_instructions(context.config) or ["set broker credentials"]
    message = " · ".join(notes).upper()
    steps = context.frames_for(context.config.scene_seconds)
    return [
        _with_marquee(frame, context, message, step, context.rows - 6) for step in range(steps)
    ]


def _with_marquee(frame: Frame, context: SceneContext, text: str, step: int, y: int) -> Frame:
    copy = frame.copy()
    copy.text(1, y, marquee(text, context.cols - 2, step), GREY)
    return copy


def _empty_card(context: SceneContext, title: str, message: str) -> List[Frame]:
    frame = context.blank()
    if context.compact:
        frame.text_centered(context.rows // 2 - 3, title, GREY)
    else:
        chrome(frame, context, title)
        frame.text_centered(context.rows // 2 - 2, message, GREY)
    return hold(frame, context.frames_for(context.config.scene_seconds))


# --- notification bursts ------------------------------------------------

def scene_event(context: SceneContext, event: DashEvent) -> List[Frame]:
    """The interrupt: a full-panel celebration or gut-punch.

    Structure is flash → reveal → settle, which reads as an *event* rather than
    just another card in the rotation. Sparkles are drawn from ``context.rng``
    so the same win always sparkles the same way.
    """
    good = event.severity == "good"
    accent = role(event.severity, WHITE)

    if context.compact:
        return _compact_event(context, event, accent)

    frames: List[Frame] = []

    # Flash: two frames of saturated colour so the change is caught peripherally.
    for level in (0.55, 0.25):
        flash = context.blank()
        flash.rect(0, 0, context.cols, context.rows, scale_color(accent, level))
        frames.append(flash)

    from . import font

    body = context.blank()

    # The headline gets as many rows as it needs. "MISSION FAILED" does not fit
    # one 52-pixel row, and squeezing the letter spacing to force it makes the
    # word unreadable at exactly the moment it matters most.
    headline = font.wrap(event.title, context.cols - 2, lines=2)
    for index, line in enumerate(headline):
        body.text_centered(index * 6, line, accent)

    body_top = len(headline) * 6 + 1
    sprite = sprites.outcome_sprite(good) if event.kind in (EventKind.WIN, EventKind.LOSS) else None
    if sprite is None and event.kind in (EventKind.FEED_DOWN, EventKind.FEED_RECOVERED):
        sprite = sprites.PLUG if event.kind is EventKind.FEED_RECOVERED else sprites.WARNING

    text_left = 2
    if sprite is not None:
        art_scale = 2 if body_top + sprite.height * 2 <= context.rows else 1
        body.blit(sprite, 2, body_top, scale=art_scale)
        text_left = 2 + sprite.width * art_scale + 3

    if event.amount is not None:
        body.text(text_left, body_top + 2, fmt.money(event.amount), accent)
    if event.detail:
        body.text(
            text_left,
            body_top + 10,
            font.fit(event.detail, context.cols - text_left - 1),
            WHITE,
        )

    reveal_steps = context.frames_for(0.4)
    for step in range(reveal_steps):
        stage = body.copy()
        stage.dim(0.55 + 0.45 * (step + 1) / reveal_steps)
        frames.append(stage)

    settle_steps = context.frames_for(1.6)
    sparkle_count = 10 if good else 0
    for step in range(settle_steps):
        stage = body.copy()
        for _ in range(sparkle_count):
            x = context.rng.randrange(context.cols)
            y = context.rng.randrange(context.rows)
            if stage.get_pixel(x, y) == BLACK:
                # Brightness is quantised to eight steps so the whole animation
                # stays inside a small colour palette — GIF only has 256 slots
                # and a continuous random ramp would blow through them.
                step_brightness = context.rng.randrange(3, 9) / 8.0
                stage.set_pixel(x, y, mix(BLACK, accent, step_brightness))
        frames.append(stage)

    return frames


def _compact_event(context: SceneContext, event: DashEvent, accent: RGB) -> List[Frame]:
    frames: List[Frame] = []
    for level in (0.7, 0.35):
        flash = context.blank()
        flash.rect(0, 0, context.cols, context.rows, scale_color(accent, level))
        frames.append(flash)
    body = context.blank()
    body.text_centered(context.rows // 2 - 6, "WIN" if event.severity == "good" else "LOSS", accent)
    if event.amount is not None:
        body.text_centered(context.rows // 2 + 1, fmt.money(event.amount), accent)
    frames.extend(hold(body, context.frames_for(1.5)))
    return frames


#: Scene id -> composer. :mod:`planner` validates plans against these keys.
COMPOSERS: Dict[str, Callable[[SceneContext], List[Frame]]] = {
    "daily": scene_daily,
    "positions": scene_positions,
    "calendar": scene_calendar,
    "agents": scene_agents,
    "error": scene_error,
    "setup": scene_setup,
}


def compose(context: SceneContext, scene_id: str) -> List[Frame]:
    composer = COMPOSERS.get(scene_id)
    if composer is None:
        return _empty_card(context, scene_id.upper()[:8], "NO SCENE")
    return composer(context)


def available_scenes() -> Tuple[str, ...]:
    return tuple(COMPOSERS)
