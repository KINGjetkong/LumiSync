r"""Reverse-engineer the per-pixel path to a Govee pixel panel.

Govee documents power, brightness, one colour and a handful of zones. The app
plainly drives all 1,664 LEDs on an H6631, so the capability exists on the wire
and is simply undocumented. This tool finds it.

It runs where the hardware is — I could not run it here — and it is deliberately
read-heavy before it is write-heavy: watch what the real app does, decode it,
then replay candidates and see which one the panel obeys.

Workflow
--------

1. ``scan`` — find the panel and confirm it answers on the LAN::

       python tools/govee_pixel_probe.py scan

2. ``watch`` — leave this running, then drive the panel from the Govee app
   (open a DIY scene, scrub brightness, start DreamView). Every UDP packet the
   panel exchanges gets logged and every ``pt`` payload decoded::

       python tools/govee_pixel_probe.py watch --save capture.jsonl

   If nothing appears, the app is talking to the panel over the cloud instead of
   the LAN. Turn on "LAN Control" in the device's settings and retry; that is
   the single most common reason this comes up empty.

3. ``diff`` — the highest-signal step. Show one solid colour in the app, capture
   it; change **one pixel**; capture again. The diff is the pixel-addressing
   format::

       python tools/govee_pixel_probe.py diff before.jsonl after.jsonl

4. ``try`` — replay each candidate encoding from
   ``lumisync/drivers/govee_pixel.py`` and watch the panel::

       python tools/govee_pixel_probe.py try --ip 10.1.1.54 --strategy chunked

   Whichever one lights the panel correctly is the protocol. Tell me which, or
   paste the ``diff`` output, and the driver's ``DEFAULT_STRATEGY`` becomes that
   with ``STRATEGY_VERIFIED = True``.

Everything here is on your own hardware and your own network.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumisync.drivers.govee_pixel import (  # noqa: E402
    CONTROL_PORT,
    LISTEN_PORT,
    MULTICAST_ADDR,
    MULTICAST_PORT,
    STRATEGIES,
    build_mode_frame,
    control_message,
    decode_pt,
    encode_grid,
)

SCAN_REQUEST = {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}
STATUS_REQUEST = {"msg": {"cmd": "status", "data": {}}}


# --- helpers ------------------------------------------------------------

def local_ipv4_addresses() -> List[str]:
    """Every IPv4 address this machine holds, best effort.

    Multicast leaves by exactly one interface — whichever the default route
    picks. On a laptop with a VPN up that is the tunnel, so a scan sent the
    obvious way goes into the tunnel and never touches the LAN the panel is on.
    Sending from every interface is what makes discovery survive that.

    ``ifconfig``/``ip``/``ipconfig`` are parsed because the alternative is a
    dependency, and this is a diagnostic tool where a best-effort list plus a
    manual ``--ip`` escape hatch is enough.
    """
    found: List[str] = []

    # The address the default route would use. No packet is actually sent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    import re
    import subprocess

    for command in (["ifconfig"], ["ip", "-4", "addr"], ["ipconfig"]):
        try:
            output = subprocess.run(
                command, capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if not output:
            continue
        for match in re.findall(r"(?:inet |IPv4 Address[.\s]*: )(\d+\.\d+\.\d+\.\d+)", output):
            if not match.startswith("127."):
                found.append(match)
        break

    ordered: List[str] = []
    for address in found:
        if address not in ordered:
            ordered.append(address)
    return ordered


def _multicast_scan(timeout: float) -> Dict[str, Dict[str, Any]]:
    """Send the discovery request out of every interface and collect replies."""
    listener = _bind(LISTEN_PORT, timeout)
    payload = json.dumps(SCAN_REQUEST).encode()
    interfaces = local_ipv4_addresses()

    try:
        for source in interfaces or [""]:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            try:
                if source:
                    sender.setsockopt(
                        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source)
                    )
                sender.sendto(payload, (MULTICAST_ADDR, MULTICAST_PORT))
            except OSError:
                # An interface that refuses multicast is normal (VPN tunnels,
                # bridges). Keep going; another one may carry it.
                pass
            finally:
                sender.close()

        return _collect_replies(listener, timeout)
    finally:
        listener.close()


def sweep_subnet(timeout: float = 4.0) -> Dict[str, Dict[str, Any]]:
    """Ask every address on the local /24 for its status.

    The fallback when multicast is blocked — by a VPN, by an access point with
    client isolation, or by a router that will not forward it. Slower and
    louder, but it only needs ordinary unicast, which survives all three.

    One socket bound to 4002 sends every probe and then listens, because this
    device family answers a status request sent *from* that port and ignores one
    from an ephemeral port.
    """
    sock = _bind(LISTEN_PORT, timeout)
    payload = json.dumps(STATUS_REQUEST).encode()

    try:
        for base in local_ipv4_addresses():
            prefix = base.rsplit(".", 1)[0]
            for host in range(1, 255):
                try:
                    sock.sendto(payload, (f"{prefix}.{host}", CONTROL_PORT))
                except OSError:
                    continue
        return _collect_replies(sock, timeout)
    finally:
        sock.close()


def _collect_replies(sock: socket.socket, timeout: float) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload, origin = sock.recvfrom(4096)
        except socket.timeout:
            break
        except OSError:
            break
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        data = (message.get("msg") or {}).get("data") or {}
        found[str(data.get("ip") or origin[0])] = data
    return found


# --- packet capture -----------------------------------------------------
#
# `watch` binds UDP 4002 and therefore only ever sees packets addressed to this
# machine — the panel's replies. It structurally cannot see what the Govee app
# sends, which is the traffic that actually answers the question. Capturing at
# the link layer is the only way to read another process's outbound packets, so
# that is what these do.

PCAP_MAGICS = {
    0xA1B2C3D4: ("<", False),   # classic, microseconds
    0xD4C3B2A1: (">", False),
    0xA1B23C4D: ("<", True),    # nanoseconds
    0x4D3CB2A1: (">", True),
}
#: Link-layer header sizes we can skip past. 0 is BSD loopback, 1 Ethernet,
#: 101 raw IP, 113 Linux "any".
LINK_HEADER_BYTES = {0: 4, 1: 14, 101: 0, 113: 16, 276: 20}


def parse_pcap(path: str) -> List[Tuple[str, str, bytes]]:
    """Extract ``(source, destination, payload)`` for every UDP packet.

    Classic pcap only — that is what ``tcpdump -w`` writes by default, and
    handling pcapng too would be a parser for a format we never ask anyone to
    produce.
    """
    import struct

    with open(path, "rb") as handle:
        blob = handle.read()

    if len(blob) < 24:
        raise ValueError("file is too short to be a pcap")

    magic = struct.unpack("<I", blob[:4])[0]
    if magic not in PCAP_MAGICS:
        magic = struct.unpack(">I", blob[:4])[0]
    if magic not in PCAP_MAGICS:
        raise ValueError(
            "not a classic pcap file. Capture with: tcpdump -w capture.pcap"
        )

    endian, _nanos = PCAP_MAGICS[magic]
    link_type = struct.unpack(endian + "I", blob[20:24])[0]
    link_bytes = LINK_HEADER_BYTES.get(link_type)
    if link_bytes is None:
        raise ValueError(f"unsupported link type {link_type}")

    packets: List[Tuple[str, str, bytes]] = []
    offset = 24
    while offset + 16 <= len(blob):
        _sec, _usec, captured, _original = struct.unpack(
            endian + "IIII", blob[offset : offset + 16]
        )
        offset += 16
        frame = blob[offset : offset + captured]
        offset += captured
        parsed = _parse_udp(frame, link_bytes, link_type)
        if parsed is not None:
            packets.append(parsed)
    return packets


def _parse_udp(frame: bytes, link_bytes: int, link_type: int) -> Optional[Tuple[str, str, bytes]]:
    """Pull a UDP payload out of one captured link-layer frame."""
    import struct

    if link_type == 1 and len(frame) >= 14:
        # Ethernet: only IPv4 carries what we want.
        if struct.unpack(">H", frame[12:14])[0] != 0x0800:
            return None

    packet = frame[link_bytes:]
    if len(packet) < 20 or (packet[0] >> 4) != 4:
        return None

    header_length = (packet[0] & 0x0F) * 4
    if packet[9] != 17 or len(packet) < header_length + 8:  # 17 = UDP
        return None

    source = ".".join(str(byte) for byte in packet[12:16])
    destination = ".".join(str(byte) for byte in packet[16:20])
    udp = packet[header_length:]
    source_port, destination_port, length, _checksum = struct.unpack(">HHHH", udp[:8])

    payload = udp[8 : max(8, length)]
    return (f"{source}:{source_port}", f"{destination}:{destination_port}", payload)


def _not_found_help(ip_hint: str = "") -> None:
    """Print every real cause, in the order they actually happen."""
    is_mac = sys.platform == "darwin"

    print("  I could not find any Govee device on this network.\n")
    print("  Work through these in order:\n")

    step = 1
    if is_mac:
        # Recent macOS blocks LAN traffic per-app until it is allowed, and it
        # fails *silently* — packets simply go nowhere. On a Mac this is the
        # single most likely cause, ahead of anything about the panel.
        print(f"   {step}. macOS is probably blocking this. It does that silently.")
        print("      System Settings -> Privacy & Security -> Local Network")
        print("      Turn ON the switch for Terminal (or iTerm/VS Code, whichever")
        print("      you are running this in). Then run this again.\n")
        step += 1

    print(f"   {step}. Is a VPN on? Turn it off.")
    print("      Discovery goes out the tunnel instead of your Wi-Fi.\n")
    step += 1

    print(f"   {step}. LAN Control still off?")
    print("      Govee Home app -> your panel -> settings -> 'LAN Control' ON\n")
    step += 1

    print(f"   {step}. Same Wi-Fi? Not guest Wi-Fi.")
    print("      If your router splits 2.4GHz and 5GHz, the panel is on 2.4GHz.\n")
    step += 1

    print(f"   {step}. Just skip discovery — this always works:")
    print("      Find the panel's IP (Govee Home -> device -> settings -> about,")
    print("      or your router's device list), then run:\n")
    print(f"        python tools/govee_pixel_probe.py wizard --ip {ip_hint or '192.168.1.50'}\n")

    addresses = local_ipv4_addresses()
    if addresses:
        print(f"  (This computer is at: {', '.join(addresses)} — the panel will")
        print("   have an address that looks similar.)")


def _bind(port: int, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:  # not portable everywhere, and not required
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", port))
    sock.settimeout(timeout)
    return sock


def _describe_packet(payload: bytes, sender: Tuple[str, int]) -> Dict[str, Any]:
    """Turn a raw datagram into a loggable record, decoding any ``pt`` inside."""
    record: Dict[str, Any] = {
        "from": f"{sender[0]}:{sender[1]}",
        "bytes": len(payload),
        "raw": payload.decode("utf-8", errors="replace"),
    }
    try:
        message = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        record["hex"] = payload.hex(" ")
        return record

    record["json"] = message
    for pt in _find_pt(message):
        frame = decode_pt(pt)
        if frame is not None:
            record.setdefault("frames", []).append(
                {
                    "opcode": f"0x{frame.opcode:02X}",
                    "length": frame.length,
                    "body_hex": frame.body.hex(" "),
                    "checksum_ok": frame.checksum_ok,
                    "length_ok": frame.length_ok,
                    "summary": frame.summary,
                }
            )
    return record


def _find_pt(node: Any) -> Iterable[str]:
    """Yield every ``pt`` string anywhere in a message."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pt" and isinstance(value, str):
                yield value
            else:
                yield from _find_pt(value)
    elif isinstance(node, list):
        for item in node:
            yield from _find_pt(item)


def _load_records(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def _frame_bodies(records: List[Dict[str, Any]]) -> List[bytes]:
    bodies: List[bytes] = []
    for record in records:
        for frame in record.get("frames") or []:
            hex_body = str(frame.get("body_hex", "")).replace(" ", "")
            if hex_body:
                try:
                    bodies.append(bytes.fromhex(hex_body))
                except ValueError:
                    continue
    return bodies


# --- commands -----------------------------------------------------------

def command_scan(args: argparse.Namespace) -> int:
    """Discover Govee devices, falling back to a subnet sweep."""
    print(f"Broadcasting scan from every interface, listening on {LISTEN_PORT}…")

    found = _multicast_scan(args.timeout)
    if not found:
        print("Multicast found nothing. Sweeping the local subnet directly…")
        found = sweep_subnet(args.timeout)

    if not found:
        print()
        _not_found_help()
        return 1

    print(f"\n{len(found)} device(s):\n")
    for ip, data in sorted(found.items()):
        sku = data.get("sku", "?")
        mac = data.get("device", "?")
        print(f"  {sku:<8} {ip:<16} {mac}")
        print(f"           --ip {ip}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    """Ask one device for its status, from the source port it expects."""
    sock = _bind(LISTEN_PORT, args.timeout)
    try:
        sock.sendto(json.dumps(STATUS_REQUEST).encode(), (args.ip, CONTROL_PORT))
        payload, origin = sock.recvfrom(4096)
    except socket.timeout:
        print(f"No reply from {args.ip}:{CONTROL_PORT} within {args.timeout:g}s.\n")
        _not_found_help(args.ip)
        return 1
    finally:
        sock.close()

    print(json.dumps(_describe_packet(payload, origin), indent=2))
    return 0


def command_watch(args: argparse.Namespace) -> int:
    """Log every packet the panel exchanges, decoding ``pt`` payloads.

    Run this, then drive the panel from the Govee app. This is the step that
    actually reveals the protocol.
    """
    sock = _bind(args.port, 1.0)
    handle = open(args.save, "a", encoding="utf-8") if args.save else None

    print(f"Listening on UDP {args.port}. Drive the panel from the Govee app now.")
    print("Open a DIY scene, change one pixel, start DreamView. Ctrl-C to stop.\n")

    count = 0
    try:
        while True:
            try:
                payload, origin = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break

            if args.ip and origin[0] != args.ip:
                continue

            record = _describe_packet(payload, origin)
            count += 1
            frames = record.get("frames") or []
            headline = frames[0]["summary"] if frames else record.get("raw", "")[:90]
            print(f"[{count:04d}] {record['from']} {record['bytes']:>5}B  {headline}")

            if handle is not None:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if handle is not None:
            handle.close()

    print(f"\n{count} packet(s) captured." + (f" Saved to {args.save}." if args.save else ""))
    if not count:
        print("Nothing seen. Two usual causes:")
        print("  1. LAN Control is off, so the app is going via the cloud.")
        print("  2. This machine is not on the path — run it on the machine running the app.")
    return 0


def command_decode(args: argparse.Namespace) -> int:
    """Decode base64 ``pt`` payloads given on the command line."""
    for value in args.payloads:
        frame = decode_pt(value)
        print(f"\n{value}")
        if frame is None:
            print("  not a recognisable frame")
            continue
        print(f"  {frame.summary}")
        print(f"  magic=0x{frame.magic:02X} length={frame.length} checksum=0x{frame.checksum:02X}")
        print(f"  body ({len(frame.body)}B): {frame.body.hex(' ')}")
    return 0


def command_diff(args: argparse.Namespace) -> int:
    """Compare two captures byte-by-byte.

    Capture a solid colour, change exactly one pixel in the app, capture again.
    The bytes that move are the pixel-addressing format — that single comparison
    usually settles the whole question.
    """
    before = _frame_bodies(_load_records(args.before))
    after = _frame_bodies(_load_records(args.after))

    print(f"{args.before}: {len(before)} frame(s)")
    print(f"{args.after}: {len(after)} frame(s)\n")

    if not before or not after:
        print("Need decoded frames in both captures. Was 'watch --save' used?")
        return 1

    if len(before) != len(after):
        print(
            f"Frame counts differ ({len(before)} vs {len(after)}) — itself a finding:\n"
            "the panel likely splits a frame across a variable number of packets.\n"
        )

    for index, (left, right) in enumerate(zip(before, after)):
        if left == right:
            continue
        print(f"frame {index}: {len(left)}B vs {len(right)}B")
        limit = min(len(left), len(right))
        changes = [offset for offset in range(limit) if left[offset] != right[offset]]
        if not changes:
            print("  same prefix, different length\n")
            continue
        print(f"  {len(changes)} byte(s) differ, first at offset {changes[0]}")
        for offset in changes[:24]:
            print(f"    [{offset:>5}]  {left[offset]:02x} -> {right[offset]:02x}")
        if len(changes) > 24:
            print(f"    … {len(changes) - 24} more")
        print()
    return 0


def command_try(args: argparse.Namespace) -> int:
    """Send a test pattern with one or every candidate encoding.

    Watch the panel. Whichever run draws the pattern correctly is the protocol.
    """
    cols, rows = args.cols, args.rows
    grid = _test_pattern(cols, rows)
    names = [args.strategy] if args.strategy else list(STRATEGIES)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    address = (args.ip, CONTROL_PORT)

    def send(message: Dict[str, Any]) -> None:
        sock.sendto(json.dumps(message).encode(), address)

    try:
        print(f"Target {args.ip}:{CONTROL_PORT}, pattern {cols}x{rows}.")
        print("Watch the panel. Corners are red/green/blue/white, centre is a cross.\n")

        send(control_message(build_mode_frame(True)))
        time.sleep(0.3)

        for name in names:
            frames = encode_grid(grid, name)
            total = sum(len(frame) for frame in frames)
            print(f"  {name:<9} {len(frames):>3} packet(s), {total:>6}B … ", end="", flush=True)

            for index, frame in enumerate(frames):
                send(control_message(frame))
                if index + 1 < len(frames):
                    time.sleep(args.pace / 1000.0)

            print("sent")
            time.sleep(args.hold)

        send(control_message(build_mode_frame(False)))
    finally:
        sock.close()

    print("\nWhich one drew the pattern? That is the encoding — tell me and it becomes")
    print("the driver default with STRATEGY_VERIFIED = True.")
    print("If none did, run 'watch --save' while the app draws, then 'diff'.")
    return 0


def _test_pattern(cols: int, rows: int) -> List[List[Tuple[int, int, int]]]:
    """A pattern whose orientation and addressing are obvious at a glance.

    Distinct corners catch a transposed or mirrored grid; the cross catches an
    off-by-one or a wrong row stride.
    """
    grid = [[(0, 0, 0) for _ in range(cols)] for _ in range(rows)]

    grid[0][0] = (255, 0, 0)                 # top-left    red
    grid[0][cols - 1] = (0, 255, 0)          # top-right   green
    grid[rows - 1][0] = (0, 0, 255)          # bottom-left blue
    grid[rows - 1][cols - 1] = (255, 255, 255)

    middle_row, middle_col = rows // 2, cols // 2
    for x in range(cols):
        grid[middle_row][x] = (40, 40, 60)
    for y in range(rows):
        grid[y][middle_col] = (40, 40, 60)
    grid[middle_row][middle_col] = (255, 180, 0)
    return grid


# --- wizard -------------------------------------------------------------
#
# One command, plain questions, no protocol knowledge required. The experiment
# is deliberately two rounds: flood a distinct colour per strategy first,
# because "which colour did it turn" is a question anyone can answer from
# across the room and it settles *whether* an encoding reaches the panel at
# all. Only then does it test addressing, and only for encodings that worked.

WIZARD_COLORS: List[Tuple[str, Tuple[int, int, int]]] = [
    ("RED", (255, 0, 0)),
    ("GREEN", (0, 255, 0)),
    ("BLUE", (0, 0, 255)),
    ("YELLOW", (255, 200, 0)),
]


def read_status(ip: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """Read a device's reported state, or None if it does not answer.

    Bound to 4002 because this family answers a status request sent *from* that
    port and ignores one from an ephemeral port.
    """
    try:
        sock = _bind(LISTEN_PORT, timeout)
    except OSError:
        return None
    try:
        sock.sendto(json.dumps(STATUS_REQUEST).encode(), (ip, CONTROL_PORT))
        payload, _origin = sock.recvfrom(4096)
        message = json.loads(payload.decode("utf-8"))
        data = (message.get("msg") or {}).get("data")
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        sock.close()


def verify_control(ip: str, send) -> Tuple[bool, str]:
    """Prove commands are landing by reading the device's own state back.

    Asking "did it look brighter" fails badly when the panel was already on and
    already bright — the honest answer is "no" even though the command worked.
    Reading ``onOff``/``brightness`` back turns that into a fact instead of a
    judgement call, so the wizard stops guessing about the operator's eyes.
    """
    before = read_status(ip)
    if before is None:
        return False, "the panel is not answering status requests at all"

    # Drive it somewhere it demonstrably is not, then read back.
    was_on = str(before.get("onOff", "")) in ("1", "True", "true")
    target_brightness = 30 if int(before.get("brightness") or 0) > 60 else 100

    send({"msg": {"cmd": "turn", "data": {"value": 0 if was_on else 1}}})
    time.sleep(1.0)
    send({"msg": {"cmd": "turn", "data": {"value": 1}}})
    send({"msg": {"cmd": "brightness", "data": {"value": target_brightness}}})
    time.sleep(1.5)

    after = read_status(ip)
    if after is None:
        return False, "the panel stopped answering after the commands"

    if after.get("brightness") != before.get("brightness") or after.get("onOff") != before.get("onOff"):
        return True, (
            f"confirmed — brightness {before.get('brightness')} -> {after.get('brightness')}, "
            f"power {before.get('onOff')} -> {after.get('onOff')}"
        )
    return False, (
        f"the panel answers, but ignored the commands "
        f"(still brightness={after.get('brightness')}, power={after.get('onOff')})"
    )


def _ask(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer.startswith("y")


def _pause(message: str = "Press Enter when ready… ") -> None:
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        print()


def command_sniff(args: argparse.Namespace) -> int:
    """Capture Govee traffic at the link layer, then decode it.

    Needs sudo, because reading another process's packets is a privileged
    operation on every OS. This is the only way to see what the Govee app
    sends — a UDP bind can only ever observe traffic addressed to this machine.
    """
    import shutil
    import subprocess

    if shutil.which("tcpdump") is None:
        print("tcpdump not found.")
        print("  macOS: it ships with the system.")
        print("  Linux: sudo apt install tcpdump")
        print("  Windows: use Wireshark, save as .pcap, then run:")
        print("    python tools/govee_pixel_probe.py parse <file.pcap>")
        return 1

    filter_expression = "udp port 4001 or udp port 4002 or udp port 4003"
    command = ["sudo", "tcpdump", "-i", args.interface, "-s", "0", "-w", args.save, filter_expression]

    print("Capturing Govee traffic. You'll be asked for your Mac password.\n")
    print("  While it runs: open the Govee app and show a picture on the panel.")
    print("  Change a colour, open a DIY scene — anything visual.\n")
    print("  Then press Ctrl-C here.\n")
    print(f"  ({' '.join(command)})\n")

    try:
        subprocess.run(command, check=False)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"Could not run tcpdump: {exc}")
        return 1

    print()
    return command_parse(argparse.Namespace(path=args.save, verbose=args.verbose))


def command_parse(args: argparse.Namespace) -> int:
    """Decode a pcap: show every Govee message and every pixel frame in it."""
    try:
        packets = parse_pcap(args.path)
    except (OSError, ValueError) as exc:
        print(f"Could not read {args.path}: {exc}")
        return 1

    if not packets:
        print(f"No UDP packets in {args.path}.")
        print("\nIf you drove the panel from your PHONE, that traffic never touches")
        print("this computer — a capture here cannot see it. Either:")
        print("  - use the Govee DESKTOP app on this machine while capturing, or")
        print("  - capture on the machine running the Govee app.")
        return 1

    print(f"{len(packets)} UDP packet(s) in {args.path}\n")
    frames_found = 0

    for source, destination, payload in packets:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if args.verbose:
                print(f"  {source} -> {destination}  {len(payload)}B (not JSON)")
            continue

        command = str((message.get("msg") or {}).get("cmd", "?"))
        print(f"  {source} -> {destination}  cmd={command}")

        for pt in _find_pt(message):
            frame = decode_pt(pt)
            frames_found += 1
            if frame is None:
                print(f"      pt (undecodable): {pt[:60]}")
                continue
            print(f"      {frame.summary}")
            if args.verbose:
                print(f"      full body: {frame.body.hex(' ')}")

    print(f"\n{frames_found} pixel/protocol frame(s) decoded.")
    if frames_found:
        print("\nThis is the answer. Send me this output (or the .pcap itself).")
    else:
        print("\nNo 'pt' frames — the app drove the panel through the cloud, not the LAN.")
        print("Turn on LAN Control for the panel and capture again.")
    return 0


def command_report(args: argparse.Namespace) -> int:
    """Run every diagnostic without asking anything, and print one paste-able block.

    This exists because the person who can reach the panel and the person
    debugging the protocol are not on the same network — and cannot be. The
    panel only answers hosts on its own LAN, so the whole diagnostic has to run
    there and travel back as text.

    Nothing here needs a decision from the operator: it measures state from the
    device itself rather than asking what they saw.
    """
    lines: List[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("===== GOVEE PIXEL PANEL REPORT =====")
    say(f"platform: {sys.platform}  python: {sys.version.split()[0]}")
    say(f"interfaces: {', '.join(local_ipv4_addresses()) or 'none found'}")
    say()

    # --- find it ---
    ip = args.ip
    if ip:
        say(f"target: {ip} (given)")
    else:
        say("scanning…")
        found = _multicast_scan(args.timeout) or sweep_subnet(args.timeout)
        if not found:
            say("RESULT: no device answered. Nothing else can run.")
            say("  Check: VPN off, LAN Control on, same Wi-Fi as the panel.")
            _write_report(args.save, lines)
            return 1
        for candidate, data in sorted(found.items()):
            say(f"  found {data.get('sku', '?')} at {candidate}")
        ip = sorted(found)[0]
        say(f"target: {ip}")
    say()

    # --- does it answer at all ---
    before = read_status(ip)
    say(f"status before: {json.dumps(before) if before else 'NO REPLY'}")
    if before is None:
        say("RESULT: the panel does not answer status requests.")
        say("  Everything below would be guesswork, so it is skipped.")
        _write_report(args.save, lines)
        return 1

    # --- do basic commands land ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(message: Dict[str, Any]) -> None:
        sock.sendto(json.dumps(message).encode(), (ip, CONTROL_PORT))

    try:
        ok, detail = verify_control(ip, send)
        say(f"basic control: {'YES' if ok else 'NO'} — {detail}")
        say()

        # --- do any pixel encodings land ---
        say("pixel encodings (each floods the panel, then state is re-read):")
        grid = [[(255, 0, 0)] * args.cols for _ in range(args.rows)]

        send(control_message(build_mode_frame(True)))
        time.sleep(0.4)

        for name in STRATEGIES:
            baseline = read_status(ip)
            frames = encode_grid(grid, name)
            for index, frame in enumerate(frames):
                send(control_message(frame))
                if index + 1 < len(frames):
                    time.sleep(0.002)
            time.sleep(1.2)
            after = read_status(ip)

            changed = bool(baseline and after and after != baseline)
            say(
                f"  {name:<9} {len(frames):>3} packet(s)  "
                f"state {'CHANGED' if changed else 'unchanged'}"
                + (f"  -> {json.dumps(after)}" if changed else "")
            )

        send(control_message(build_mode_frame(False)))
    finally:
        sock.close()

    say()
    say("===== END REPORT =====")
    say("Send this whole block back. Also watch the panel while it runs —")
    say("if any colour appeared, say which line it happened on.")

    _write_report(args.save, lines)
    return 0


def _write_report(path: str, lines: List[str]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"\n(also saved to {path})")
    except OSError:
        pass


def command_wizard(args: argparse.Namespace) -> int:
    """Walk the whole experiment start to finish, asking plain questions."""
    print("=" * 62)
    print("  GOVEE PIXEL PANEL — FINDING THE PER-PIXEL PATH")
    print("=" * 62)
    print(
        "\nThis runs a few light patterns on your panel and asks what you saw."
        "\nNo technical knowledge needed. Takes about two minutes.\n"
    )

    # --- step 1: find the panel ---
    ip = args.ip
    if not ip:
        print("STEP 1 — Finding your panel on the network…\n")
        ip = _wizard_find_panel(args.timeout)
        if not ip:
            return 1
    else:
        print(f"STEP 1 — Using the panel you gave me: {ip}\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    address = (ip, CONTROL_PORT)

    def send(message: Dict[str, Any]) -> None:
        sock.sendto(json.dumps(message).encode(), address)

    try:
        # --- step 2: prove we can talk to it at all ---
        # Measured, not eyeballed. A panel that was already on and already
        # bright looks identical after a "turn on and brighten" command, so
        # asking the operator what they saw would fail a working setup.
        print("\nSTEP 2 — Checking I can control the panel.")
        print("It should blink off and back on. Reading its state to be sure…\n")

        ok, detail = verify_control(ip, send)
        print(f"  {detail}\n")

        if not ok:
            if "not answering" in detail:
                print("  It answered discovery but not a direct status request.")
                print("  Usually the panel is at a different address than it advertised.")
                print("  Check your router for the panel's real IP, then:\n")
                print("    python tools/govee_pixel_probe.py wizard --ip <that address>\n")
                return 1

            print("  The panel is reachable but ignoring commands. Two usual causes:\n")
            print("   1. It is running a scene/mode from the app that overrides")
            print("      external control. Set it to a plain solid colour in Govee")
            print("      Home first, then run this again.")
            print("   2. It is a model that only accepts LAN commands while the app")
            print("      is closed. Force-quit Govee Home and retry.\n")
            if not _ask("  Try the picture tests anyway?", default=True):
                return 1
        else:
            print("  Good — commands are landing. Now the real test.\n")

        # --- step 3: which encoding reaches the panel ---
        print("-" * 62)
        print("STEP 3 — I'll try 4 different ways to send a picture.")
        print("Each one floods the WHOLE panel with a different colour.")
        print("Just note which colours actually show up.\n")
        for index, (label, _rgb) in enumerate(WIZARD_COLORS, start=1):
            print(f"   Try {index}: {label}")
        print("\nSome may do nothing at all. That is expected and useful.")
        _pause()

        send(control_message(build_mode_frame(True)))
        time.sleep(0.4)

        names = list(STRATEGIES)
        for index, name in enumerate(names):
            label, rgb = WIZARD_COLORS[index % len(WIZARD_COLORS)]
            grid = [[rgb] * args.cols for _ in range(args.rows)]
            frames = encode_grid(grid, name)

            print(f"\n  Sending {label}…  ({len(frames)} packet(s))")
            for packet_index, frame in enumerate(frames):
                send(control_message(frame))
                if packet_index + 1 < len(frames):
                    time.sleep(args.pace / 1000.0)
            time.sleep(args.hold)

        send(control_message(build_mode_frame(False)))

        print("\n" + "-" * 62)
        print("Which colours did you actually see on the panel?\n")
        working: List[str] = []
        for index, name in enumerate(names):
            label, _rgb = WIZARD_COLORS[index % len(WIZARD_COLORS)]
            if _ask(f"  Did you see {label}?"):
                working.append(name)

        if not working:
            return _wizard_no_luck(ip)

        # --- step 4: orientation, only for what worked ---
        print("\n" + "-" * 62)
        print(f"STEP 4 — {len(working)} way(s) worked. Now checking the picture is")
        print("the right way round.\n")
        print("I'll draw a pattern with 4 different corners:")
        print("   top-left RED · top-right GREEN")
        print("   bottom-left BLUE · bottom-right WHITE")
        print("   plus an ORANGE dot dead centre.\n")
        _pause()

        send(control_message(build_mode_frame(True)))
        time.sleep(0.4)

        correct: List[str] = []
        for name in working:
            frames = encode_grid(_test_pattern(args.cols, args.rows), name)
            print(f"\n  Drawing with method '{name}'…")
            for packet_index, frame in enumerate(frames):
                send(control_message(frame))
                if packet_index + 1 < len(frames):
                    time.sleep(args.pace / 1000.0)
            time.sleep(args.hold + 1.0)

            if _ask("  Corners in the right places (red top-left)?"):
                correct.append(name)

        send(control_message(build_mode_frame(False)))
        return _wizard_report(working, correct)
    finally:
        sock.close()


def _wizard_find_panel(timeout: float) -> str:
    """Discover the panel, or explain in plain words why nothing answered."""
    found = _multicast_scan(timeout)
    if not found:
        print("  Nothing answered the broadcast. Trying every address directly…")
        found = sweep_subnet(timeout)

    if not found:
        print()
        _not_found_help()
        return ""

    if len(found) == 1:
        ip = next(iter(found))
        sku = str(found[ip].get("sku", "") or "device")
        print(f"  Found it: {sku} at {ip}")
        # Auto-picking the only device is convenient but wrong if that device
        # is a strip and the panel never answered — say what it is so a bad
        # pick is obvious rather than silent.
        if sku.upper() not in ("H6631",) and sku != "device":
            print(f"\n  Heads up: {sku} is not the pixel panel model I know about.")
            if not _ask(f"  Is {sku} your pixel panel?", default=True):
                print("\n  Then the panel did not answer. Find its IP in your router")
                print("  or the Govee app, and run:")
                print("    python tools/govee_pixel_probe.py wizard --ip <that address>")
                return ""
        return ip

    print(f"  Found {len(found)} devices:\n")
    options = sorted(found)
    for index, candidate in enumerate(options, start=1):
        print(f"   {index}. {found[candidate].get('sku', '?'):<8} {candidate}")
    try:
        choice = input("\n  Which number is the pixel panel? ").strip()
        return options[int(choice) - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("  Not a valid choice.")
        return ""


def _wizard_no_luck(ip: str) -> int:
    print("\n" + "=" * 62)
    print("  NONE OF THEM WORKED — and that is still useful.")
    print("=" * 62)
    print(
        "\nThe panel takes basic commands but ignores all four picture formats,"
        "\nso it uses something different. One more capture finds it.\n"
    )
    print("Do this:\n")
    print("  1. Open a terminal and run:")
    print("       python tools/govee_pixel_probe.py watch --save capture.jsonl\n")
    print("  2. Leave it running. On your phone, open Govee Home and")
    print("     show ANY picture on the panel (a DIY scene works).\n")
    print("  3. Press Ctrl-C in the terminal.\n")
    print("  4. Send me capture.jsonl.\n")
    print(f"(Panel address, in case you need it: {ip})")
    return 2


def _wizard_report(working: List[str], correct: List[str]) -> int:
    print("\n" + "=" * 62)
    print("  DONE — here is what to send me")
    print("=" * 62 + "\n")

    if correct:
        print(f"  WORKING METHOD: {correct[0]}")
        if len(correct) > 1:
            print(f"  (also worked: {', '.join(correct[1:])})")
        print("\n  Copy that line and send it to me. That is all I need —")
        print("  the panel will stream your live dashboard after that.")
        return 0

    print(f"  Showed colour: {', '.join(working)}")
    print("  But the picture came out wrong / scrambled.\n")
    print("  Send me the line above. Scrambled is close — it means the data")
    print("  is landing and only the pixel order is off, which is a small fix.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="govee_pixel_probe",
        description="Find the undocumented per-pixel path to a Govee pixel panel.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    wizard = sub.add_parser(
        "wizard", help="START HERE — does everything and asks you simple questions"
    )
    wizard.add_argument("--ip", default="", help="skip discovery and use this address")
    wizard.add_argument("--cols", type=int, default=52)
    wizard.add_argument("--rows", type=int, default=32)
    wizard.add_argument("--timeout", type=float, default=4.0)
    wizard.add_argument("--pace", type=float, default=2.0, help="ms between packets")
    wizard.add_argument("--hold", type=float, default=3.0, help="seconds to show each attempt")
    wizard.set_defaults(func=command_wizard)

    report = sub.add_parser(
        "report", help="run every check automatically and print one block to send back"
    )
    report.add_argument("--ip", default="", help="skip discovery and use this address")
    report.add_argument("--cols", type=int, default=52)
    report.add_argument("--rows", type=int, default=32)
    report.add_argument("--timeout", type=float, default=4.0)
    report.add_argument("--save", default="govee-report.txt")
    report.set_defaults(func=command_report)

    scan = sub.add_parser("scan", help="discover Govee devices on the LAN")
    scan.add_argument("--timeout", type=float, default=3.0)
    scan.set_defaults(func=command_scan)

    status = sub.add_parser("status", help="query one device's status")
    status.add_argument("--ip", required=True)
    status.add_argument("--timeout", type=float, default=3.0)
    status.set_defaults(func=command_status)

    watch = sub.add_parser("watch", help="log packets while the Govee app drives the panel")
    watch.add_argument("--port", type=int, default=LISTEN_PORT)
    watch.add_argument("--ip", default="", help="only log packets from this address")
    watch.add_argument("--save", default="", help="append decoded records to this JSONL file")
    watch.set_defaults(func=command_watch)

    sniff = sub.add_parser(
        "sniff", help="capture what the Govee APP sends (needs sudo) and decode it"
    )
    sniff.add_argument("--interface", default="any", help="network interface (macOS: en0)")
    sniff.add_argument("--save", default="govee.pcap")
    sniff.add_argument("--verbose", action="store_true")
    sniff.set_defaults(func=command_sniff)

    parse_cmd = sub.add_parser("parse", help="decode a .pcap you already captured")
    parse_cmd.add_argument("path")
    parse_cmd.add_argument("--verbose", action="store_true")
    parse_cmd.set_defaults(func=command_parse)

    decode = sub.add_parser("decode", help="decode base64 pt payloads")
    decode.add_argument("payloads", nargs="+")
    decode.set_defaults(func=command_decode)

    diff = sub.add_parser("diff", help="compare two captures to find the pixel format")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.set_defaults(func=command_diff)

    attempt = sub.add_parser("try", help="replay candidate encodings against the panel")
    attempt.add_argument("--ip", required=True)
    attempt.add_argument("--cols", type=int, default=52)
    attempt.add_argument("--rows", type=int, default=32)
    attempt.add_argument("--strategy", default="", choices=[""] + list(STRATEGIES))
    attempt.add_argument("--pace", type=float, default=2.0, help="ms between packets")
    attempt.add_argument("--hold", type=float, default=3.0, help="seconds to hold each attempt")
    attempt.set_defaults(func=command_try)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
