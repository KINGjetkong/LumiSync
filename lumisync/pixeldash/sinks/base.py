"""The sink interface and its report type."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from ..render.pipeline import RenderResult


@dataclass(frozen=True)
class SinkReport:
    """What a sink actually managed to do.

    ``degraded`` is the important field. A sink that was asked for per-pixel
    output and could only deliver an average colour has not failed — but the
    operator needs to know the panel is showing an approximation, so it says so
    here instead of reporting a clean success.
    """

    sink: str
    ok: bool
    detail: str = ""
    degraded: bool = False
    artifacts: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def failure(cls, sink: str, detail: str) -> "SinkReport":
        return cls(sink=sink, ok=False, detail=detail)

    @property
    def summary(self) -> str:
        state = "ok" if self.ok else "FAILED"
        if self.ok and self.degraded:
            state = "degraded"
        return f"{self.sink}: {state}{f' — {self.detail}' if self.detail else ''}"


class Sink(abc.ABC):
    """One destination for a rendered dashboard."""

    name: str = "sink"

    #: Geometry this sink wants, as a key into
    #: :data:`~lumisync.pixeldash.models.KNOWN_TARGETS`. ``""`` means "whatever
    #: the config's panel target is".
    #:
    #: A monitor is not a panel: an LED matrix has a fixed pixel count, but a
    #: hover overlay can draw as many pixels as it likes, and a denser grid is
    #: what buys the larger, more legible font. So the screen sinks ask for
    #: ``screen`` while the panel sink stays at hardware resolution, and the
    #: service renders once per distinct geometry.
    target: str = ""

    @abc.abstractmethod
    def publish(self, result: RenderResult) -> SinkReport:
        """Send a render onward. Must not raise — return a failing report."""

    def close(self) -> None:
        """Release resources. Safe to call more than once."""

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def publish_all(sinks: Sequence[Sink], result: RenderResult) -> List[SinkReport]:
    """Fan a render out to every sink, isolating failures.

    A sink that raises despite the contract is caught here — a broken output
    path must never take down the poll loop that feeds the others.
    """
    reports: List[SinkReport] = []
    for sink in sinks:
        try:
            reports.append(sink.publish(result))
        except Exception as exc:
            reports.append(SinkReport.failure(sink.name, f"{type(exc).__name__}: {exc}"))
    return reports


def group_by_target(sinks: Sequence[Sink], default: str) -> Dict[str, List[Sink]]:
    """Bucket sinks by the geometry they want, so each is rendered once.

    Two sinks asking for the same geometry share a single render; a sink with
    no preference joins the default bucket.
    """
    groups: Dict[str, List[Sink]] = {}
    for sink in sinks:
        groups.setdefault(getattr(sink, "target", "") or default, []).append(sink)
    return groups


def close_all(sinks: Sequence[Sink]) -> None:
    for sink in sinks:
        try:
            sink.close()
        except Exception:
            pass
