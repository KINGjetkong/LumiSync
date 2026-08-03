"""Write the render to disk.

This is the sink that always works, and on the Govee Gaming Pixel Light it is
currently the *only* path that puts a real per-pixel dashboard on the panel:
the panel's own app imports GIFs, and Govee's documented LAN API exposes only
power, brightness and a single colour — no per-pixel surface. So the GIF on
disk is a first-class output, not a debug artifact.

It also gives every other tool a stable file to watch: OBS, a wallpaper engine,
a browser source, or the ghost display.
"""

from __future__ import annotations

import os
from typing import Optional

from ..render.pipeline import RenderResult, write_bundle
from .base import Sink, SinkReport


class FileSink(Sink):
    """Writes ``<name>.gif``, ``<name>.png`` and ``<name>.json``.

    Filenames are stable so downstream consumers can hold one path forever.
    Optionally also writes a digest-named copy into ``history/`` so a specific
    render can be pulled back up from its manifest weeks later.
    """

    name = "files"

    def __init__(
        self,
        output_dir: str,
        *,
        basename: str = "dashboard",
        scale: int = 1,
        keep_history: bool = False,
    ) -> None:
        self.output_dir = output_dir
        self.basename = basename
        self.scale = max(1, int(scale))
        self.keep_history = keep_history

    def publish(self, result: RenderResult) -> SinkReport:
        try:
            paths = write_bundle(result, self.output_dir, name=self.basename)
            if self.keep_history:
                history = self._write_history(result)
                if history:
                    paths["history"] = history
        except OSError as exc:
            return SinkReport.failure(self.name, f"write failed: {exc}")

        return SinkReport(
            sink=self.name,
            ok=True,
            detail=f"{result.frame_count} frames -> {os.path.basename(paths['gif'])}",
            artifacts=paths,
        )

    def _write_history(self, result: RenderResult) -> Optional[str]:
        directory = os.path.join(self.output_dir, "history")
        path = os.path.join(directory, f"{result.digest}.gif")
        if os.path.exists(path):
            # Same digest, same bytes — rewriting would be pure churn.
            return path
        return result.write_gif(path, scale=self.scale)
