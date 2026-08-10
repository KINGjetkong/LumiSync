"""Govee pixel-panel driver — a per-pixel path the vendor does not document.

Govee's public surface tops out at power, brightness, one colour, and up to a
few zones. A 52x32 panel like the H6631 has 1,664 addressable LEDs and the app
plainly drives every one of them, so a per-pixel path exists on the wire; it is
simply not written down. This module is the attempt to reach it.

What is confirmed (``docs/govee-lan-research-and-implementation.md``, from a real
capture): control is plain JSON over UDP to port 4003, and the segment stream is
a base64 ``pt`` payload carrying a ``0xBB`` frame::

    BB  len_hi len_lo  B0  01  count  r g b  r g b  ...  xor

Two things about that frame decide the whole problem:

* The **length field is 16-bit**, so it can describe 4,992 bytes of pixel data
  without modification. The frame format is not the limit.
* The **count field is 8-bit**, so one frame cannot name more than 255
  segments. That is the limit, and it is why a 1,664-pixel panel must be doing
  something else — a wider count, a chunk offset, or many smaller packets.

So this driver implements the plausible encodings as separate, pure functions
and lets one be selected. Each is a real guess with a reason, not a placeholder:

``single``
    One ``B0`` frame with the full pixel run. Tests whether ``count`` is really
    8-bit or whether the firmware reads the 16-bit length instead.
``chunked``
    ``B0`` frames carrying a start offset and a slice of pixels. This is how
    almost every long-strip protocol handles more segments than a byte can
    count.
``rows``
    One frame per panel row, offset by row index — chunking on a boundary the
    firmware is likely to already understand for a 2D panel.
``groups``
    One packet per distinct colour with an explicit coordinate list, which is
    exactly how the iDotMatrix panels in this repo do DIY drawing. Cheap for
    pixel art, which is mostly flat colour.

**None of these is confirmed against hardware.** ``tools/govee_pixel_probe.py``
is the other half of this: it captures what the real app sends, decodes it into
these same structures, and replays candidates so the right one can be
identified. When it is, ``DEFAULT_STRATEGY`` changes and
:class:`GoveePixelAdapter` starts streaming live frames — the sink above it
already handles everything else.

Until then the adapter reports its strategy as unverified, and nothing in Pixel
Dash claims the panel is showing a real dashboard.
"""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .base import RGB, DeviceCapabilities, TransportAdapter

# --- wire constants (confirmed) -----------------------------------------
CONTROL_PORT = 4003
LISTEN_PORT = 4002
MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 4001

FRAME_MAGIC = 0xBB
#: Segment-colour opcode, confirmed from LumiSync's working strip control.
OP_SEGMENT = 0xB0
#: Razer/DreamView mode toggle, confirmed: [BB 00 01 B1 <on> xor].
OP_MODE = 0xB1

#: The 8-bit count field's ceiling — the reason a 1,664-pixel panel needs more
#: than one frame under the known encoding.
MAX_SEGMENTS_PER_FRAME = 255

#: Conservative payload budget per datagram. Above roughly this size the IP
#: layer fragments, and embedded network stacks routinely drop fragments.
SAFE_PAYLOAD_BYTES = 1200


def xor_checksum(data: Iterable[int]) -> int:
    """The trailing checksum byte on every Govee frame."""
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def clamp_rgb(color: Sequence[int]) -> Tuple[int, int, int]:
    return tuple(max(0, min(255, int(channel))) for channel in color[:3])


#: Bytes in a frame that are not body: magic, two length bytes, opcode, checksum.
FRAME_OVERHEAD = 5


def build_frame(opcode: int, body: Sequence[int]) -> bytes:
    """Assemble ``BB <len16> <opcode> <body...> <xor>``.

    ``len`` counts **the body only** — not the opcode, not itself, not the
    checksum. Both confirmed frames agree on this: the mode toggle
    ``BB 00 01 B1 01 0A`` declares 1 for a one-byte body, and the segment frame
    declares ``2 + n * 3`` for a body of ``[flag, count, ...rgb]``.
    """
    payload = [int(byte) & 0xFF for byte in body]
    length = len(payload)
    frame = [FRAME_MAGIC, (length >> 8) & 0xFF, length & 0xFF, opcode & 0xFF, *payload]
    frame.append(xor_checksum(frame))
    return bytes(frame)


def build_mode_frame(enabled: bool) -> bytes:
    """The confirmed Razer/DreamView toggle.

    Reproduces LumiSync's working ``switch_razer`` byte-for-byte; a segment
    stream is ignored unless this mode is on.
    """
    frame = bytearray([FRAME_MAGIC, 0x00, 0x01, OP_MODE, 0x01 if enabled else 0x00])
    frame.append(xor_checksum(frame))
    return bytes(frame)


def encode_pt(frame: bytes) -> str:
    return base64.b64encode(frame).decode("ascii")


def control_message(payload: bytes, command: str = "razer") -> Dict[str, Any]:
    """Wrap a raw frame in the JSON envelope the device expects on 4003."""
    return {"msg": {"cmd": command, "data": {"pt": encode_pt(payload)}}}


# --- candidate per-pixel encodings --------------------------------------

def flatten(grid: Sequence[Sequence[RGB]]) -> List[Tuple[int, int, int]]:
    """Row-major pixel list from a 2D grid."""
    return [clamp_rgb(pixel) for row in grid for pixel in row]


def encode_single(grid: Sequence[Sequence[RGB]], **_options) -> List[bytes]:
    """One frame for the whole panel.

    Deliberately writes the pixel count into a 16-bit field rather than the
    8-bit one the strip protocol uses. If the firmware reads the frame's own
    length rather than the count byte, this is the entire protocol; if it reads
    the count byte, the panel will ignore or garble this and we learn that too.
    """
    pixels = flatten(grid)
    body = [0x01, (len(pixels) >> 8) & 0xFF, len(pixels) & 0xFF]
    for color in pixels:
        body.extend(color)
    return [build_frame(OP_SEGMENT, body)]


def encode_chunked(
    grid: Sequence[Sequence[RGB]],
    *,
    chunk: int = 0,
    **_options,
) -> List[bytes]:
    """Frames carrying a 16-bit start offset and a slice of pixels.

    The standard answer for addressing more segments than one byte can count,
    and the shape most long-strip protocols converge on. Chunk size defaults to
    whatever keeps a datagram under the fragmentation threshold.
    """
    pixels = flatten(grid)
    # Two ceilings, and both matter. The count stays an 8-bit field — widening
    # the *address* is the whole reason this encoding has an offset, so the
    # per-frame count has no reason to change — and the datagram still has to
    # clear the fragmentation threshold.
    size = chunk or min(MAX_SEGMENTS_PER_FRAME, max(1, (SAFE_PAYLOAD_BYTES - 8) // 3))
    size = max(1, min(size, MAX_SEGMENTS_PER_FRAME))

    frames: List[bytes] = []
    for start in range(0, len(pixels), size):
        window = pixels[start : start + size]
        body = [
            0x02,                       # sub-op: offset write
            (start >> 8) & 0xFF,
            start & 0xFF,
            len(window) & 0xFF,
        ]
        for color in window:
            body.extend(color)
        frames.append(build_frame(OP_SEGMENT, body))
    return frames


def encode_rows(grid: Sequence[Sequence[RGB]], **_options) -> List[bytes]:
    """One frame per panel row, tagged with its row index.

    Chunking on a boundary a 2D panel's firmware is likely to already know
    about. A 52-wide row is 156 bytes of colour — comfortably inside one
    datagram and inside the 8-bit count field.
    """
    frames: List[bytes] = []
    for index, row in enumerate(grid):
        pixels = [clamp_rgb(pixel) for pixel in row]
        body = [0x03, index & 0xFF, len(pixels) & 0xFF]
        for color in pixels:
            body.extend(color)
        frames.append(build_frame(OP_SEGMENT, body))
    return frames


def encode_groups(grid: Sequence[Sequence[RGB]], *, skip_black: bool = True, **_options) -> List[bytes]:
    """One packet per distinct colour, each listing its (x, y) coordinates.

    This is how the iDotMatrix driver in this repo draws, and it suits pixel art
    specifically: a dashboard frame is mostly flat colour, so a scene with a
    dozen colours costs a dozen small packets instead of five kilobytes.
    """
    groups: Dict[Tuple[int, int, int], List[Tuple[int, int]]] = {}
    for y, row in enumerate(grid):
        for x, pixel in enumerate(row):
            color = clamp_rgb(pixel)
            if skip_black and color == (0, 0, 0):
                continue
            groups.setdefault(color, []).append((x, y))

    frames: List[bytes] = []
    # Sorted so the same grid always produces the same packets, matching the
    # determinism the renderer upstream guarantees.
    for color, coords in sorted(groups.items()):
        for start in range(0, len(coords), (SAFE_PAYLOAD_BYTES - 8) // 2):
            window = coords[start : start + (SAFE_PAYLOAD_BYTES - 8) // 2]
            body = [0x04, *color, (len(window) >> 8) & 0xFF, len(window) & 0xFF]
            for x, y in window:
                body.extend((x & 0xFF, y & 0xFF))
            frames.append(build_frame(OP_SEGMENT, body))
    return frames


Strategy = Callable[..., List[bytes]]

#: Every candidate encoding, by name. ``tools/govee_pixel_probe.py try`` walks
#: this table against real hardware.
STRATEGIES: Dict[str, Strategy] = {
    "single": encode_single,
    "chunked": encode_chunked,
    "rows": encode_rows,
    "groups": encode_groups,
}

#: Chunked is the default guess: it is the shape long-strip protocols converge
#: on, and it degrades gracefully if the offset semantics are wrong (you get a
#: partial image rather than nothing, which is itself diagnostic).
DEFAULT_STRATEGY = "chunked"

#: Flipped to True only when a capture confirms an encoding against hardware.
#: Everything downstream reads this rather than assuming the panel works.
STRATEGY_VERIFIED = False


def encode_grid(grid: Sequence[Sequence[RGB]], strategy: str = DEFAULT_STRATEGY, **options) -> List[bytes]:
    """Encode a 2D pixel grid with the named strategy."""
    encoder = STRATEGIES.get(strategy)
    if encoder is None:
        raise ValueError(f"unknown strategy {strategy!r}; known: {sorted(STRATEGIES)}")
    return encoder(grid, **options)


# --- frame decoding (for the probe tool) --------------------------------

@dataclass(frozen=True)
class DecodedFrame:
    """A parsed Govee frame, for reading captures back."""

    magic: int
    length: int
    opcode: int
    body: bytes
    checksum: int
    checksum_ok: bool
    length_ok: bool

    @property
    def summary(self) -> str:
        flags = []
        if not self.checksum_ok:
            flags.append("BAD-XOR")
        if not self.length_ok:
            flags.append("BAD-LEN")
        suffix = f"  [{' '.join(flags)}]" if flags else ""
        return (
            f"op=0x{self.opcode:02X} len={self.length} body={len(self.body)}B"
            f" {self.body[:16].hex(' ')}{'…' if len(self.body) > 16 else ''}{suffix}"
        )


def decode_frame(payload: bytes) -> Optional[DecodedFrame]:
    """Parse a raw ``pt`` frame. Returns None if it is not frame-shaped.

    Checksum and length are *reported*, never enforced — a frame that fails
    either is exactly the interesting one when reverse-engineering, and
    throwing it away would discard the finding.
    """
    if len(payload) < 5 or payload[0] != FRAME_MAGIC:
        return None

    length = (payload[1] << 8) | payload[2]
    opcode = payload[3]
    body = payload[4:-1]
    checksum = payload[-1]

    return DecodedFrame(
        magic=payload[0],
        length=length,
        opcode=opcode,
        body=bytes(body),
        checksum=checksum,
        checksum_ok=xor_checksum(payload[:-1]) == checksum,
        length_ok=length == len(payload) - FRAME_OVERHEAD,
    )


def decode_pt(value: str) -> Optional[DecodedFrame]:
    """Decode a base64 ``pt`` string straight out of a captured JSON message."""
    try:
        return decode_frame(base64.b64decode(value, validate=False))
    except Exception:
        return None


# --- transport ----------------------------------------------------------

class GoveePixelAdapter(TransportAdapter):
    """Drive a Govee pixel panel over LAN UDP with a per-pixel encoding.

    ``device`` needs ``ip`` and should carry ``matrix_size`` as ``"52x32"``.
    ``strategy`` selects the encoding; ``pace_ms`` throttles multi-packet frames
    so a panel with a small receive queue is not flooded.
    """

    def __init__(self, device: Dict[str, Any], server: Optional[socket.socket] = None) -> None:
        super().__init__(device)
        self._size = _parse_size(device.get("matrix_size"), default=(52, 32))
        self._strategy = str(device.get("strategy") or DEFAULT_STRATEGY)
        self._pace = max(0.0, float(device.get("pace_ms", 2))) / 1000.0
        self._owns_socket = server is None
        self._socket = server or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._streaming = False

    @property
    def capabilities(self) -> DeviceCapabilities:
        cols, rows = self._size
        return DeviceCapabilities(
            transport="lan",
            segment_count=cols * rows,
            supports_power=True,
            supports_brightness=True,
            supports_color=True,
            supports_segments=True,
            matrix_size=self._size,
        )

    @property
    def strategy(self) -> str:
        return self._strategy

    def set_strategy(self, name: str) -> None:
        if name not in STRATEGIES:
            raise ValueError(f"unknown strategy {name!r}; known: {sorted(STRATEGIES)}")
        self._strategy = name

    def describe(self) -> str:
        state = "verified" if STRATEGY_VERIFIED else "UNVERIFIED"
        cols, rows = self._size
        return f"{cols}x{rows} via '{self._strategy}' ({state})"

    # --- transport ---
    def _address(self) -> Tuple[str, int]:
        ip = str(self.device.get("ip") or "")
        if not ip:
            raise RuntimeError("Govee pixel device has no 'ip' to send to.")
        return (ip, CONTROL_PORT)

    def _send_json(self, message: Dict[str, Any]) -> None:
        self._socket.sendto(json.dumps(message).encode("utf-8"), self._address())

    def _send_frames(self, frames: Sequence[bytes]) -> None:
        import time

        for index, frame in enumerate(frames):
            self._send_json(control_message(frame))
            if self._pace and index + 1 < len(frames):
                time.sleep(self._pace)

    # --- control surface ---
    def set_power(self, on: bool) -> None:
        self._send_json({"msg": {"cmd": "turn", "data": {"value": int(bool(on))}}})

    def set_brightness(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        self._send_json({"msg": {"cmd": "brightness", "data": {"value": value}}})

    def set_color(self, r: int, g: int, b: int) -> None:
        red, green, blue = clamp_rgb((r, g, b))
        self._send_json(
            {
                "msg": {
                    "cmd": "colorwc",
                    "data": {"color": {"r": red, "g": green, "b": blue}, "colorTemInKelvin": 0},
                }
            }
        )

    def set_segments(self, colors: List[RGB]) -> None:
        """Push a flat colour run as a single-row grid."""
        self.draw_grid([[clamp_rgb(color) for color in colors]])

    def draw_grid(self, grid: Sequence[Sequence[RGB]], clear: bool = True) -> None:
        """Push a full 2D pixel frame.

        This is the method the matrix sink probes for. Its presence is what
        makes :func:`lumisync.pixeldash.sinks.matrix.probe` resolve to per-pixel
        mode, so it must only exist on a driver that genuinely attempts one.
        """
        self._send_frames(encode_grid(grid, self._strategy))

    def begin_stream(self) -> None:
        if not self._streaming:
            self._send_json(control_message(build_mode_frame(True)))
            self._streaming = True

    def end_stream(self) -> None:
        if self._streaming:
            self._send_json(control_message(build_mode_frame(False)))
            self._streaming = False

    def query_status(self) -> Optional[Dict[str, Any]]:
        """Ask the device for its status, listening on the port it replies to.

        The capture notes that this device family answers a probe sent *from*
        port 4002 and ignores one from an ephemeral port, so the reply socket
        binds that port explicitly.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("", LISTEN_PORT))
            probe.settimeout(2.0)
            probe.sendto(
                json.dumps({"msg": {"cmd": "status", "data": {}}}).encode(), self._address()
            )
            payload, _sender = probe.recvfrom(4096)
            return json.loads(payload.decode("utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            probe.close()

    def close(self) -> None:
        if self._owns_socket and self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None


def _parse_size(value: Any, default: Tuple[int, int]) -> Tuple[int, int]:
    """Parse ``"52x32"`` into ``(cols, rows)``."""
    text = str(value or "").lower().replace(" ", "")
    if "x" in text:
        left, _, right = text.partition("x")
        if left.isdigit() and right.isdigit():
            return (int(left), int(right))
    return default
