"""Deterministic pixel-art rendering for the dashboard."""

from .canvas import Frame
from .pipeline import RenderResult, render, snapshot_digest

__all__ = ["Frame", "RenderResult", "render", "snapshot_digest"]
