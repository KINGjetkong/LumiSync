"""Chooses *what* to show; the composers decide *how* to draw it.

This is the "AI-assisted, deterministic" seam. The split matters:

* A **plan** is a small, validated structure — scene order, headline copy,
  accent colour. It is the only thing a model is ever allowed to influence.
* Plans are **content-addressed**. The key is a digest of the decision-relevant
  shape of the snapshot (is the day green, are feeds up, which symbols are
  open), not the snapshot's exact numbers. So the same situation reuses the
  same cached plan and the render stays byte-identical.
* The model therefore runs **at most once per novel situation**, off the
  polling path, and never at all if a plan is cached or no hook is configured.
  The fallback is a deterministic rule table, so the dashboard is fully
  functional with no model attached.

Anything a hook returns is validated against the known scene ids and clamped
before use. A hook cannot invent a scene, inject an unbounded string, or put a
number on the panel — numbers only ever come from the feeds.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..config import PixelDashConfig
from ..models import DashboardSnapshot, DataClass, FeedState
from .palette import CYAN, GREEN, RED, VIOLET, RGB

#: Every scene the composer knows how to draw. A plan may only reference these.
SCENE_IDS: Tuple[str, ...] = (
    "daily",
    "positions",
    "calendar",
    "agents",
    "error",
    "setup",
)

MAX_HEADLINE = 13  # characters that fit one 52px row at 3x5 with tracking
PlannerHook = Callable[[Dict], Optional[Dict]]


@dataclass(frozen=True)
class ScenePlan:
    """The validated instruction set for one render."""

    scenes: Tuple[str, ...]
    headline: str = ""
    accent: RGB = VIOLET
    digest: str = ""
    source: str = "rules"      # rules | cache | hook
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> Dict:
        return {
            "scenes": list(self.scenes),
            "headline": self.headline,
            "accent": list(self.accent),
            "source": self.source,
            "notes": list(self.notes),
        }


def plan_digest(snapshot: DashboardSnapshot) -> str:
    """Digest of the *situation*, not the exact numbers.

    Deliberately coarse. P&L is reduced to its sign and order of magnitude so a
    dollar of drift does not invalidate a cached plan, while a swing from green
    to red does.
    """
    today = snapshot.today
    shape = {
        "class": snapshot.data_class.value,
        "feeds": sorted((status.name, status.state.value) for status in snapshot.feeds),
        "traded": bool(today and today.trades),
        "sign": _sign(today.realized) if today else 0,
        "magnitude": _magnitude(today.realized) if today else 0,
        "wins": min(today.wins, 9) if today else 0,
        "losses": min(today.losses, 9) if today else 0,
        "symbols": sorted({position.symbol for position in snapshot.positions}),
        "agents": sorted((agent.agent_id, agent.state) for agent in snapshot.agents),
        "days": min(len(snapshot.days), 90),
    }
    payload = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2s(payload, digest_size=16).hexdigest()


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _magnitude(value: float) -> int:
    """Order of magnitude bucket, so cache keys survive small drift."""
    magnitude = abs(value)
    for index, threshold in enumerate((100, 500, 1_000, 5_000, 25_000, 100_000)):
        if magnitude < threshold:
            return index
    return 6


def rule_plan(snapshot: DashboardSnapshot, config: PixelDashConfig) -> ScenePlan:
    """The deterministic default. Always available, never needs a model."""
    digest = plan_digest(snapshot)
    accent = RED if snapshot.data_class is DataClass.LIVE else CYAN

    unconfigured = [
        status for status in snapshot.feeds if status.state is FeedState.UNCONFIGURED
    ]
    failed = [status for status in snapshot.feeds if status.state is FeedState.ERROR]

    if unconfigured and not any(status.ok for status in snapshot.feeds):
        return ScenePlan(
            scenes=("setup",),
            headline="SETUP",
            accent=accent,
            digest=digest,
            notes=tuple(status.detail for status in unconfigured),
        )

    scenes: List[str] = ["error"] if failed else []
    for scene in config.rotation:
        if scene not in SCENE_IDS or scene in ("error", "setup"):
            continue
        if scene == "positions" and not snapshot.positions:
            continue
        if scene == "calendar" and not snapshot.days:
            continue
        if scene == "agents" and not snapshot.agents:
            continue
        scenes.append(scene)
    if not scenes:
        scenes = ["daily"]

    today = snapshot.today
    if today is None or today.trades == 0:
        headline = "NO TRADES"
    elif today.realized > 0:
        headline = "GREEN DAY"
    elif today.realized < 0:
        headline = "RED DAY"
    else:
        headline = "FLAT DAY"

    return ScenePlan(
        scenes=tuple(scenes),
        headline=headline,
        accent=GREEN if (today and today.realized > 0) else accent,
        digest=digest,
        source="rules",
    )


def plan(
    snapshot: DashboardSnapshot,
    config: PixelDashConfig,
    *,
    hook: Optional[PlannerHook] = None,
) -> ScenePlan:
    """Resolve a plan: cache, then hook, then rules.

    Any failure anywhere in the chain silently falls back to the rule plan —
    the panel must keep rendering real numbers whether or not a model is
    reachable.
    """
    baseline = rule_plan(snapshot, config)
    if not config.planner_enabled:
        return baseline

    cached = load_plan(config.plan_cache_dir, baseline.digest)
    if cached is not None:
        return cached

    hook = hook or load_hook()
    if hook is None:
        return baseline

    try:
        raw = hook(planner_context(snapshot, baseline))
    except Exception:
        return baseline

    resolved = validate(raw, baseline)
    if resolved is not baseline:
        save_plan(config.plan_cache_dir, resolved)
    return resolved


def planner_context(snapshot: DashboardSnapshot, baseline: ScenePlan) -> Dict:
    """The prompt payload handed to a hook.

    Note what is *not* in here: no account identifiers, no position sizes, no
    dollar amounts. A planner decides framing, not content, so it does not need
    the numbers — and keeping them out means a hook can be a remote service
    without exporting the account.
    """
    today = snapshot.today
    return {
        "digest": baseline.digest,
        "allowed_scenes": list(SCENE_IDS),
        "default": baseline.to_json(),
        "situation": {
            "data_class": snapshot.data_class.value,
            "traded_today": bool(today and today.trades),
            "day_sign": _sign(today.realized) if today else 0,
            "wins": today.wins if today else 0,
            "losses": today.losses if today else 0,
            "open_positions": len(snapshot.positions),
            "feeds_ok": all(status.ok for status in snapshot.feeds),
            "agent_states": sorted({agent.state for agent in snapshot.agents}),
        },
        "constraints": {
            "max_headline_chars": MAX_HEADLINE,
            "headline_charset": "A-Z 0-9 space and . , : - + ! ?",
        },
    }


def validate(raw: Optional[Dict], baseline: ScenePlan) -> ScenePlan:
    """Coerce a hook's output into a safe plan, or return the baseline."""
    if not isinstance(raw, dict):
        return baseline

    scenes = tuple(
        scene
        for scene in raw.get("scenes", ())
        if isinstance(scene, str) and scene in SCENE_IDS
    )
    if not scenes:
        scenes = baseline.scenes

    headline = raw.get("headline", baseline.headline)
    if not isinstance(headline, str):
        headline = baseline.headline
    headline = "".join(
        char for char in headline.upper() if char.isalnum() or char in " .,:-+!?"
    )[:MAX_HEADLINE].strip()

    accent = _validate_color(raw.get("accent"), baseline.accent)

    return ScenePlan(
        scenes=scenes,
        headline=headline or baseline.headline,
        accent=accent,
        digest=baseline.digest,
        source="hook",
    )


def _validate_color(value, fallback: RGB) -> RGB:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return fallback
    try:
        channels = tuple(max(0, min(255, int(channel))) for channel in value)
    except (TypeError, ValueError):
        return fallback
    # A near-black accent would render as nothing; refuse it.
    return channels if sum(channels) >= 90 else fallback


# --- plan cache ---------------------------------------------------------

def plan_path(cache_dir: str, digest: str) -> str:
    return os.path.join(cache_dir, f"{digest}.json")


def load_plan(cache_dir: str, digest: str) -> Optional[ScenePlan]:
    if not cache_dir or not digest:
        return None
    try:
        with open(plan_path(cache_dir, digest), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    scenes = tuple(
        scene for scene in raw.get("scenes", ()) if isinstance(scene, str) and scene in SCENE_IDS
    )
    if not scenes:
        return None
    accent = _validate_color(raw.get("accent"), VIOLET)
    return ScenePlan(
        scenes=scenes,
        headline=str(raw.get("headline", ""))[:MAX_HEADLINE],
        accent=accent,
        digest=digest,
        source="cache",
    )


def save_plan(cache_dir: str, resolved: ScenePlan) -> None:
    if not cache_dir or not resolved.digest:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(plan_path(cache_dir, resolved.digest), "w", encoding="utf-8") as handle:
            json.dump(resolved.to_json(), handle, indent=2, sort_keys=True)
    except OSError:
        # A read-only cache directory degrades to "ask the hook every time",
        # which is slower but still correct.
        pass


def load_hook(spec: str = "") -> Optional[PlannerHook]:
    """Import a planner hook from ``module:function``.

    Configured with ``PIXELDASH_PLANNER_HOOK``. Absent or unimportable means no
    hook, which is the supported default rather than an error.
    """
    spec = spec or os.environ.get("PIXELDASH_PLANNER_HOOK", "")
    if not spec or ":" not in spec:
        return None
    module_name, _, attribute = spec.partition(":")
    try:
        import importlib

        module = importlib.import_module(module_name)
        hook = getattr(module, attribute, None)
    except Exception:
        return None
    return hook if callable(hook) else None


def describe(plan_value: ScenePlan) -> str:
    """One-line provenance string, shown in the CLI and the GUI status row."""
    return f"{plan_value.source}:{plan_value.digest[:8]} [{' '.join(plan_value.scenes)}]"


def scenes_for(plan_value: ScenePlan, available: Sequence[str]) -> Tuple[str, ...]:
    """Intersect a plan with the scenes the composer actually implements."""
    return tuple(scene for scene in plan_value.scenes if scene in available) or ("daily",)
