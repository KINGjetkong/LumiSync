"""Where a rendered dashboard goes.

Sinks are independent and best-effort: one failing never stops the others, and
each reports what it actually did rather than what it was asked to do. That
distinction matters most for :mod:`~lumisync.pixeldash.sinks.matrix`, where the
hardware decides how much of a frame it can really display.
"""

from .base import Sink, SinkReport
from .files import FileSink

__all__ = ["FileSink", "Sink", "SinkReport"]
