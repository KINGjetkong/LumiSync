"""GIF encoding, built for pixel-exactness and byte-level reproducibility.

Three choices are load-bearing:

* **Nearest-neighbour upscaling.** A pixel panel's whole aesthetic is hard
  square edges. Any smoothing filter destroys it.
* **An exact global palette** when the animation uses 256 colours or fewer,
  which it almost always does. Adaptive per-frame quantisation would let the
  encoder pick slightly different palettes for visually identical frames.
* **``optimize=False``.** Pillow's GIF optimiser rewrites frames as diffs
  against their predecessor. That is smaller, but it makes the output sensitive
  to encoder version in ways that break "the same snapshot renders the same
  bytes".
"""

from __future__ import annotations

import io
import os
from typing import List, Sequence, Tuple

import numpy as np

from .canvas import Frame

GIF_PALETTE_LIMIT = 256


def unique_colors(frames: Sequence[Frame]) -> List[Tuple[int, int, int]]:
    """Every distinct colour across the animation, in a stable order.

    Sorted rather than first-seen so two runs that generate frames in a
    different internal order still produce the same palette.
    """
    seen = set()
    for frame in frames:
        pixels = frame.data.reshape(-1, 3)
        seen.update(map(tuple, np.unique(pixels, axis=0).tolist()))
    return sorted(seen)


def _build_palette_image(colors: Sequence[Tuple[int, int, int]]):
    from PIL import Image

    flat: List[int] = []
    for color in colors:
        flat.extend(int(channel) for channel in color)
    flat.extend([0] * (768 - len(flat)))

    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(flat)
    return palette_image


def frames_to_images(frames: Sequence[Frame], scale: int = 1):
    """Convert frames to PIL images at ``scale``x, nearest-neighbour."""
    from PIL import Image

    return [Image.fromarray(frame.upscale(scale), mode="RGB") for frame in frames]


def encode(
    frames: Sequence[Frame],
    *,
    frame_ms: int = 120,
    scale: int = 1,
    loop: int = 0,
) -> bytes:
    """Encode frames into GIF bytes.

    Raises ``ValueError`` on an empty animation rather than writing a zero-frame
    file that every downstream consumer would have to special-case.
    """
    if not frames:
        raise ValueError("cannot encode an empty animation")

    images = frames_to_images(frames, scale=scale)
    colors = unique_colors(frames)

    if len(colors) <= GIF_PALETTE_LIMIT:
        palette_image = _build_palette_image(colors)
        quantized = [
            image.quantize(palette=palette_image, dither=0) for image in images
        ]
    else:
        # More colours than GIF can hold. Median-cut once on the first frame and
        # reuse that palette for the rest so the animation stays consistent.
        reference = images[0].quantize(colors=GIF_PALETTE_LIMIT, method=2, dither=0)
        quantized = [image.quantize(palette=reference, dither=0) for image in images]

    buffer = io.BytesIO()
    quantized[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=quantized[1:],
        duration=max(20, int(frame_ms)),
        loop=int(loop),
        optimize=False,
        disposal=1,
    )
    return buffer.getvalue()


def write(
    frames: Sequence[Frame],
    path: str,
    *,
    frame_ms: int = 120,
    scale: int = 1,
    loop: int = 0,
) -> str:
    """Encode and write atomically; returns the path written."""
    payload = encode(frames, frame_ms=frame_ms, scale=scale, loop=loop)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(payload)
    os.replace(temporary, path)
    return path


def write_png(frame: Frame, path: str, *, scale: int = 1) -> str:
    """Write a single frame as a PNG — used for still previews and thumbnails."""
    from PIL import Image

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    image = Image.fromarray(frame.upscale(scale), mode="RGB")
    temporary = f"{path}.tmp"
    image.save(temporary, format="PNG", optimize=False)
    os.replace(temporary, path)
    return path


def decode_frames(payload: bytes) -> List[Frame]:
    """Read a GIF back into frames. Used by the tests and the hover preview."""
    from PIL import Image, ImageSequence

    frames: List[Frame] = []
    with Image.open(io.BytesIO(payload)) as image:
        for page in ImageSequence.Iterator(image):
            rgb = np.asarray(page.convert("RGB"), dtype=np.uint8)
            frame = Frame(rgb.shape[1], rgb.shape[0])
            frame.data = rgb.copy()
            frames.append(frame)
    return frames
