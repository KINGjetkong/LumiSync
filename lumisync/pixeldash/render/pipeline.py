"""Snapshot -> frames -> GIF, deterministically.

The contract this module exists to enforce: **the same snapshot always renders
to the same bytes.** That is what lets a frame be reproduced from a journal
entry weeks after the fact, and it is why every stochastic-looking flourish in
the scenes draws from a generator seeded by :func:`snapshot_digest`.

Nothing here reads a clock. The only time that reaches a frame is
``snapshot.captured_at``, which the collector stamped.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import PixelDashConfig
from ..events import DashEvent
from ..models import DashboardSnapshot
from . import gif, planner, scenes
from .canvas import Frame
from .planner import PlannerHook, ScenePlan


def _canonical(value: Any) -> Any:
    """Reduce a snapshot to JSON-safe primitives with a stable ordering."""
    from dataclasses import asdict, is_dataclass
    from enum import Enum

    if is_dataclass(value) and not isinstance(value, type):
        return {key: _canonical(item) for key, item in sorted(asdict(value).items())}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, set) else items
    if isinstance(value, float):
        # Round to a hundredth of a cent. Two snapshots that differ only by
        # float noise must not produce different renders.
        return round(value, 4)
    return value


def snapshot_digest(snapshot: DashboardSnapshot, extra: Any = None) -> str:
    """Content hash of everything that can affect the rendered output."""
    payload = {"snapshot": _canonical(snapshot)}
    if extra is not None:
        payload["extra"] = _canonical(extra)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2s(encoded, digest_size=16).hexdigest()


def seed_from(digest: str) -> int:
    return int(digest[:16], 16)


@dataclass
class RenderResult:
    """The rendered animation plus everything needed to explain it."""

    frames: List[Frame]
    digest: str
    plan: ScenePlan
    scene_ids: Tuple[str, ...]
    frame_ms: int
    events: Tuple[DashEvent, ...] = field(default_factory=tuple)
    _gif: Optional[bytes] = None

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def duration_ms(self) -> int:
        return self.frame_count * self.frame_ms

    def gif_bytes(self, *, scale: int = 1, loop: int = 0) -> bytes:
        """Encode once and cache — sinks commonly ask for the same bytes."""
        if scale == 1 and loop == 0:
            if self._gif is None:
                self._gif = gif.encode(self.frames, frame_ms=self.frame_ms)
            return self._gif
        return gif.encode(self.frames, frame_ms=self.frame_ms, scale=scale, loop=loop)

    def write_gif(self, path: str, *, scale: int = 1) -> str:
        return gif.write(self.frames, path, frame_ms=self.frame_ms, scale=scale)

    def write_still(self, path: str, *, scale: int = 1) -> str:
        return gif.write_png(self.frames[0], path, scale=scale)

    def manifest(self) -> Dict[str, Any]:
        """Sidecar metadata written next to the GIF.

        This is the provenance record: which snapshot, which plan, which scenes,
        and whether a model influenced the framing.
        """
        return {
            "digest": self.digest,
            "frames": self.frame_count,
            "frame_ms": self.frame_ms,
            "duration_ms": self.duration_ms,
            "scenes": list(self.scene_ids),
            "plan": self.plan.to_json(),
            "plan_source": self.plan.source,
            "events": [
                {"kind": event.kind.value, "key": event.key, "title": event.title}
                for event in self.events
            ],
        }


def render(
    snapshot: DashboardSnapshot,
    config: PixelDashConfig,
    *,
    events: Sequence[DashEvent] = (),
    plan: Optional[ScenePlan] = None,
    hook: Optional[PlannerHook] = None,
) -> RenderResult:
    """Compose the full animation for one snapshot.

    Event bursts play first and interrupt the rotation — a fill that just
    printed matters more than the calendar. The rotation then plays in the
    order the plan specifies.
    """
    events = tuple(events)
    resolved_plan = plan if plan is not None else planner.plan(snapshot, config, hook=hook)
    digest = snapshot_digest(snapshot, extra=[event.key for event in events])
    rng = random.Random(seed_from(digest))

    context = scenes.build_context(snapshot, config, resolved_plan, rng)

    frames: List[Frame] = []
    for event in events:
        frames.extend(scenes.scene_event(context, event))

    scene_ids = planner.scenes_for(resolved_plan, scenes.available_scenes())
    for scene_id in scene_ids:
        frames.extend(scenes.compose(context, scene_id))

    if not frames:
        # Should be unreachable — ``scenes_for`` guarantees at least one scene —
        # but a zero-frame animation would fail encoding, so refuse loudly.
        raise RuntimeError("render produced no frames")

    return RenderResult(
        frames=frames,
        digest=digest,
        plan=resolved_plan,
        scene_ids=scene_ids,
        frame_ms=int(config.frame_ms),
        events=events,
    )


def render_event(
    snapshot: DashboardSnapshot,
    config: PixelDashConfig,
    event: DashEvent,
    *,
    plan: Optional[ScenePlan] = None,
) -> RenderResult:
    """Render a single notification burst with no rotation behind it."""
    resolved_plan = plan or planner.rule_plan(snapshot, config)
    digest = snapshot_digest(snapshot, extra=event.key)
    rng = random.Random(seed_from(digest))
    context = scenes.build_context(snapshot, config, resolved_plan, rng)

    return RenderResult(
        frames=scenes.scene_event(context, event),
        digest=digest,
        plan=resolved_plan,
        scene_ids=("event",),
        frame_ms=int(config.frame_ms),
        events=(event,),
    )


def write_bundle(result: RenderResult, output_dir: str, *, name: str = "dashboard") -> Dict[str, str]:
    """Write the GIF, a still and the manifest into ``output_dir``.

    The GIF lands at a stable filename so anything watching the folder — the
    ghost display, a Govee Home import, an OBS source — always points at the
    same path.
    """
    os.makedirs(output_dir, exist_ok=True)
    paths = {
        "gif": os.path.join(output_dir, f"{name}.gif"),
        "still": os.path.join(output_dir, f"{name}.png"),
        "manifest": os.path.join(output_dir, f"{name}.json"),
    }
    result.write_gif(paths["gif"])
    result.write_still(paths["still"])

    temporary = f"{paths['manifest']}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result.manifest(), handle, indent=2, sort_keys=True)
    os.replace(temporary, paths["manifest"])
    return paths
