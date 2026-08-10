"""Scene composers — snapshot in, frames out.

Every composer is a pure function of its :class:`SceneContext`. Nothing here
reads a clock, opens a socket, or calls ``random`` directly; the only source of
variation is ``context.rng``, which the pipeline seeds from the snapshot digest.
That is what makes a render reproducible.

Layout comes from ``context.metrics``, computed from the target — never from
pixel constants in this file. That is what lets the same scenes lay out on a
16x16 panel, on the 52x32 H6631, and on a 156x96 screen surface where the
denser grid earns the larger 5x7 face. Below 32 columns the composers switch to
a stripped-down variant: a 16x16 panel can show a number and a colour, and
pretending otherwise just produces mush.
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
from .metrics import POSITION_ROWS, Metrics, metrics_for
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

#: Longest calendar window, in weeks. Past this the columns are too thin to
#: read and the window outruns the trader's working memory anyway.
MAX_CALENDAR_WEEKS = 16


@dataclass(frozen=True)
class SceneContext:
    """Everything a composer is allowed to see."""

    snapshot: DashboardSnapshot
    target: RenderTarget
    metrics: Metrics
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
        return self.metrics.compact

    @property
    def face(self):
        return self.metrics.face

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
    *,
    target: Optional[RenderTarget] = None,
) -> SceneContext:
    """Assemble the context for one render.

    ``target`` overrides the config's panel geometry, which is how the same
    snapshot renders once at 52x32 for the panel and again at 104x64 for the
    screen surfaces in a single pass.
    """
    resolved = target or config.target
    return SceneContext(
        snapshot=snapshot,
        target=resolved,
        metrics=metrics_for(resolved),
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

    metrics = context.metrics
    face = metrics.face

    if right_text is None:
        right_text = context.snapshot.data_class.short
        right_color = RED if context.snapshot.data_class is DataClass.LIVE else CYAN
    right_color = right_color or GREY

    right_width = face.text_width(right_text) if right_text else 0
    title_space = metrics.content_width - right_width - 2
    frame.text(metrics.left, 0, face.fit(title, title_space), context.plan.accent, face=face)
    if right_text:
        frame.text_right(metrics.right, 0, right_text, right_color, face=face)
    frame.hline(0, metrics.rule_y, context.cols, scale_color(context.plan.accent, 0.30))


def marquee(text: str, width: int, offset: int, face=None) -> str:
    """Window into a horizontally scrolling string.

    Returns ``text`` unchanged when it already fits, so short labels never
    wobble.
    """
    from . import font

    face = face or font.DEFAULT
    if face.text_width(text) <= width:
        return text
    padded = f"{text}   "
    start = offset % len(padded)
    rotated = padded[start:] + padded[:start]
    return face.fit(rotated, width)


# --- daily --------------------------------------------------------------

def scene_daily(context: SceneContext) -> List[Frame]:
    """The headline card: today's realized P&L, record and session split."""
    metrics = context.metrics
    face = metrics.face
    summary = context.summary
    frame = context.blank()

    realized = summary.realized_today
    color = pnl_color(realized) if realized is not None else GREY
    text = fmt.money(realized)

    if context.compact:
        size = fmt.fit_scale(text, context.cols - 2, preferred=2, face=face)
        frame.text_centered(
            (context.rows - face.height * size) // 2, text, color, scale=size, face=face
        )
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "TODAY")

    size = fmt.fit_scale(
        text, metrics.content_width, preferred=metrics.hero_scale, face=face
    )
    hero_y = metrics.body_top
    frame.text_centered(hero_y, text, color, scale=size, face=face)

    record_y = hero_y + face.height * size + 3
    if metrics.fits(record_y, face.height):
        if summary.trades_today:
            record = f"{summary.wins_today}W {summary.losses_today}L"
            frame.text(metrics.left, record_y, record, WHITE, face=face)
            rate = fmt.percent(summary.win_rate_today, signed=False)
            frame.text_right(
                metrics.right, record_y, rate, _rate_color(summary.win_rate_today), face=face
            )
        else:
            # No closed trades yet is a real state, and it is not "flat".
            frame.text(
                metrics.left,
                record_y,
                face.fit("NO CLOSES YET", metrics.content_width),
                GREY,
                face=face,
            )

    session_y = record_y + metrics.line_height
    if metrics.fits(session_y, face.height):
        _session_split(frame, context, session_y)

    # Taller targets have room for a fourth line. It is only drawn when it
    # genuinely fits — the panel layout must not depend on it existing.
    context_y = session_y + metrics.line_height
    if metrics.fits(context_y, face.height):
        _standing(frame, context, context_y)

    return hold(frame, context.frames_for(context.config.scene_seconds))


def _standing(frame: Frame, context: SceneContext, y: int) -> None:
    """Streak on the left, open exposure on the right.

    Both answer "where do I stand right now" rather than "what happened
    today", which is what the rest of the card already covers.
    """
    metrics = context.metrics
    face = metrics.face
    summary = context.summary

    streak = summary.streak
    streak_color = GREY
    if streak.length:
        streak_color = role("good") if streak.winning else role("bad")
    frame.text(metrics.left, y, streak.label, streak_color, face=face)

    if summary.open_positions:
        open_text = f"{summary.open_positions} OPEN"
        if summary.open_unrealized is not None:
            open_text = f"{summary.open_positions}:{fmt.money(summary.open_unrealized)}"
            color = pnl_color(summary.open_unrealized)
        else:
            color = GREY
        frame.text_right(
            metrics.right, y, face.fit(open_text, metrics.content_width - 12), color, face=face
        )
    else:
        frame.text_right(metrics.right, y, "FLAT", GREY, face=face)


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
    metrics = context.metrics
    face = metrics.face
    summary = context.summary
    gap = face.glyph_width("R") + 2

    frame.text(metrics.left, y, "R", GREY, face=face)
    frame.text(
        metrics.left + gap, y, fmt.money(summary.rth_today), pnl_color(summary.rth_today), face=face
    )

    middle = context.cols // 2 + metrics.margin
    frame.text(middle, y, "E", GREY, face=face)
    frame.text(
        middle + gap, y, fmt.money(summary.eth_today), pnl_color(summary.eth_today), face=face
    )


# --- positions ----------------------------------------------------------

def scene_positions(context: SceneContext) -> List[Frame]:
    """Open risk, biggest first, paged a few at a time."""
    metrics = context.metrics
    positions = list(context.snapshot.positions)
    if not positions:
        return _empty_card(context, "OPEN", "NO POSITIONS")

    rows_available = max(1, (context.rows - metrics.body_top) // metrics.row_pitch)
    per_page = min(POSITION_ROWS, rows_available)
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
            _position_row(frame, context, position, metrics.body_top + index * metrics.row_pitch)
        frames.extend(hold(frame, context.frames_for(seconds_per_page)))
    return frames


def _position_row(frame: Frame, context: SceneContext, position: Position, y: int) -> None:
    metrics = context.metrics
    face = metrics.face

    label = fmt.short_symbol(position.symbol, position.underlying)
    change = position.unrealized_pct
    right = fmt.percent(change, digits=0) if change is not None else fmt.MISSING
    color = pnl_color(change) if change is not None else GREY

    right_width = face.text_width(right)
    frame.text(
        metrics.left,
        y,
        face.fit(label, metrics.content_width - right_width - 2),
        WHITE,
        face=face,
    )
    frame.text_right(metrics.right, y, right, color, face=face)

    # A proportional bar makes several positions comparable at a glance without
    # asking anyone to read several numbers.
    bar_y = y + face.height + 1
    if metrics.fits(bar_y, 1) and change is not None:
        span = metrics.content_width
        filled = int(round(min(1.0, abs(change) / 100.0) * span))
        frame.hline(metrics.left, bar_y, span, UNTRADED)
        if filled:
            frame.hline(metrics.left, bar_y, filled, scale_color(color, 0.85))


# --- calendar -----------------------------------------------------------

def scene_calendar(context: SceneContext) -> List[Frame]:
    """The journal heat grid: seven rows of weekdays, weeks running across.

    Colour encodes the day's realized P&L against the window's biggest day, so
    the grid self-scales to the account. Untraded days stay near-black — an
    unlit square means "no trades", never "flat".
    """
    metrics = context.metrics
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

    top = metrics.body_top
    weeks = max(1, min(metrics.content_width // metrics.cell_pitch, MAX_CALENDAR_WEEKS))

    end = context.snapshot.captured_at.date()
    grid = recent_grid(days, end, weeks=weeks)
    scale_value = heat_scale(days)

    for column, week in enumerate(grid):
        for row, cell in enumerate(week):
            x = metrics.left + column * metrics.cell_pitch
            y = top + row * metrics.cell_pitch
            if not metrics.fits(y, metrics.cell):
                continue
            if cell.stats is None or cell.stats.trades == 0:
                frame.rect(
                    x, y, metrics.cell, metrics.cell, UNTRADED if cell.in_month else BLACK
                )
                continue
            color = heat_color(heat_level(cell.stats.realized, scale_value))
            frame.rect(x, y, metrics.cell, metrics.cell, color)
            if cell.date == end:
                # Ring today so the eye finds "now" in the grid immediately.
                frame.outline(x - 1, y - 1, metrics.cell + 2, metrics.cell + 2, WHITE)

    return hold(frame, context.frames_for(context.config.scene_seconds))


# --- agents -------------------------------------------------------------

def scene_agents(context: SceneContext) -> List[Frame]:
    """A row of agent avatars, focus stepping through them.

    This is the "something moved in the workflow" surface: each agent's body
    colour is its state, so a red silhouette in the row is visible long before
    anyone reads the label.
    """
    metrics = context.metrics
    face = metrics.face
    agents = list(context.snapshot.agents)
    if not agents:
        return _empty_card(context, "AGENTS", "NONE")

    art_scale = 1 if metrics.avatar_pitch < 12 else 2
    pitch = metrics.avatar_pitch
    visible = agents[: max(1, metrics.content_width // pitch)]
    seconds_each = context.config.scene_seconds / len(visible)
    frames: List[Frame] = []

    for focus_index, focused in enumerate(visible):
        frame = context.blank()
        chrome(frame, context, "AGENTS")

        avatar_height = 0
        for index, agent in enumerate(visible):
            x = metrics.left + index * pitch
            sprite = sprites.agent_sprite(agent.agent_id, agent.state)
            frame.blit(sprite, x, metrics.body_top, scale=art_scale)
            avatar_height = sprite.height * art_scale
            if index == focus_index:
                marker_y = metrics.body_top + avatar_height + 1
                if metrics.fits(marker_y, 1):
                    frame.hline(x, marker_y, sprite.width * art_scale, context.plan.accent)

        label_y = metrics.body_top + avatar_height + 3
        if metrics.fits(label_y, face.height):
            frame.text(metrics.left, label_y, focused.label, WHITE, face=face)
            state_color = sprites.AGENT_STATE_COLORS.get(focused.state.lower(), GREY)
            frame.text_right(
                metrics.right, label_y, focused.state.upper()[:4], state_color, face=face
            )

        message_y = label_y + metrics.line_height
        if metrics.fits(message_y, face.height) and focused.message:
            steps = context.frames_for(seconds_each)
            for step in range(steps):
                scrolled = frame.copy()
                scrolled.text(
                    metrics.left,
                    message_y,
                    marquee(focused.message.upper(), metrics.content_width, step, face),
                    GREY,
                    face=face,
                )
                frames.append(scrolled)
            continue

        frames.extend(hold(frame, context.frames_for(seconds_each)))
    return frames


# --- failure states -----------------------------------------------------

def scene_error(context: SceneContext) -> List[Frame]:
    """Draw the broken feeds. This card exists so a dead feed is never silent."""
    metrics = context.metrics
    face = metrics.face
    failed = context.snapshot.failed_feeds
    frame = context.blank()

    if context.compact:
        frame.rect(0, 0, context.cols, context.rows, (40, 0, 0))
        frame.text_centered(context.rows // 2 - 3, "FEED", RED, face=face)
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "FEED DOWN", right_text="!", right_color=RED)

    art_scale = 1 if face.height <= 5 else 2
    frame.blit(sprites.WARNING, metrics.left + 1, metrics.body_top, scale=art_scale)

    left = metrics.left + 1 + sprites.WARNING.width * art_scale + 3
    names = ", ".join(status.name.upper() for status in failed) or "UNKNOWN"
    frame.text(left, metrics.body_top, face.fit(names, context.cols - left - 1), RED, face=face)

    detail = failed[0].detail if failed else "no detail"
    detail_y = context.rows - face.height - 1
    steps = context.frames_for(context.config.scene_seconds)
    return [
        _with_marquee(frame, context, detail.upper(), step, detail_y) for step in range(steps)
    ]


def scene_setup(context: SceneContext) -> List[Frame]:
    """Shown when no broker is configured — the only card with no numbers on it."""
    from ..config import setup_instructions

    metrics = context.metrics
    face = metrics.face
    frame = context.blank()

    if context.compact:
        frame.text_centered(context.rows // 2 - 3, "SET UP", CYAN, face=face)
        return hold(frame, context.frames_for(context.config.scene_seconds))

    chrome(frame, context, "SETUP", right_text="")

    art_scale = 1 if face.height <= 5 else 2
    frame.blit(sprites.PLUG, metrics.left + 1, metrics.body_top, scale=art_scale)
    left = metrics.left + 1 + sprites.PLUG.width * art_scale + 3
    frame.text(
        left, metrics.body_top, face.fit("NO BROKER", context.cols - left - 1), CYAN, face=face
    )

    notes = setup_instructions(context.config) or ["set broker credentials"]
    message = " · ".join(notes).upper()
    detail_y = context.rows - face.height - 1
    steps = context.frames_for(context.config.scene_seconds)
    return [
        _with_marquee(frame, context, message, step, detail_y) for step in range(steps)
    ]


def _with_marquee(frame: Frame, context: SceneContext, text: str, step: int, y: int) -> Frame:
    metrics = context.metrics
    copy = frame.copy()
    copy.text(
        metrics.left,
        y,
        marquee(text, metrics.content_width, step, metrics.face),
        GREY,
        face=metrics.face,
    )
    return copy


def _empty_card(context: SceneContext, title: str, message: str) -> List[Frame]:
    face = context.metrics.face
    frame = context.blank()
    if context.compact:
        frame.text_centered(context.rows // 2 - 3, title, GREY, face=face)
    else:
        chrome(frame, context, title)
        frame.text_centered(context.rows // 2 - face.height // 2, message, GREY, face=face)
    return hold(frame, context.frames_for(context.config.scene_seconds))


# --- notification bursts ------------------------------------------------

def scene_event(context: SceneContext, event: DashEvent) -> List[Frame]:
    """The interrupt: a full-panel celebration or gut-punch.

    Structure is flash → reveal → settle, which reads as an *event* rather than
    just another card in the rotation. Sparkles are drawn from ``context.rng``
    so the same win always sparkles the same way.
    """
    metrics = context.metrics
    face = metrics.face
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

    body = context.blank()

    # The headline gets as many rows as it needs. "MISSION FAILED" does not fit
    # one 52-pixel row, and squeezing the letter spacing to force it makes the
    # word unreadable at exactly the moment it matters most.
    headline = face.wrap(event.title, metrics.content_width, lines=2)
    for index, line in enumerate(headline):
        body.text_centered(index * metrics.line_height, line, accent, face=face)

    body_top = len(headline) * metrics.line_height + 1
    sprite = sprites.outcome_sprite(good) if event.kind in (EventKind.WIN, EventKind.LOSS) else None
    if sprite is None and event.kind in (EventKind.FEED_DOWN, EventKind.FEED_RECOVERED):
        sprite = sprites.PLUG if event.kind is EventKind.FEED_RECOVERED else sprites.WARNING

    text_left = metrics.left + 1
    if sprite is not None:
        art_scale = max(1, (context.rows - body_top) // sprite.height)
        art_scale = min(art_scale, 4)
        body.blit(sprite, metrics.left + 1, body_top, scale=art_scale)
        text_left = metrics.left + 1 + sprite.width * art_scale + 3

    if event.amount is not None:
        body.text(text_left, body_top + 2, fmt.money(event.amount), accent, face=face)
    if event.detail:
        body.text(
            text_left,
            body_top + 2 + metrics.line_height,
            face.fit(event.detail, context.cols - text_left - 1),
            WHITE,
            face=face,
        )

    reveal_steps = context.frames_for(0.4)
    for step in range(reveal_steps):
        stage = body.copy()
        stage.dim(0.55 + 0.45 * (step + 1) / reveal_steps)
        frames.append(stage)

    settle_steps = context.frames_for(1.6)
    sparkle_count = max(6, context.cols // 5) if good else 0
    for _step in range(settle_steps):
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
    face = context.metrics.face
    frames: List[Frame] = []
    for level in (0.7, 0.35):
        flash = context.blank()
        flash.rect(0, 0, context.cols, context.rows, scale_color(accent, level))
        frames.append(flash)
    body = context.blank()
    body.text_centered(
        context.rows // 2 - 6, "WIN" if event.severity == "good" else "LOSS", accent, face=face
    )
    if event.amount is not None:
        body.text_centered(context.rows // 2 + 1, fmt.money(event.amount), accent, face=face)
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
