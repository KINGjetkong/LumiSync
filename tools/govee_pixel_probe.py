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
    """Multicast-discover Govee devices and print what answers."""
    print(f"Broadcasting scan to {MULTICAST_ADDR}:{MULTICAST_PORT}, listening on {LISTEN_PORT}…")

    listener = _bind(LISTEN_PORT, args.timeout)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    found: Dict[str, Dict[str, Any]] = {}
    try:
        sender.sendto(json.dumps(SCAN_REQUEST).encode(), (MULTICAST_ADDR, MULTICAST_PORT))
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                payload, origin = listener.recvfrom(4096)
            except socket.timeout:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            data = (message.get("msg") or {}).get("data") or {}
            ip = str(data.get("ip") or origin[0])
            found[ip] = data
    finally:
        listener.close()
        sender.close()

    if not found:
        print("\nNothing answered.")
        print("Most likely: LAN Control is off. Govee Home -> device -> settings -> LAN Control.")
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
        print(f"No reply from {args.ip}:{CONTROL_PORT} within {args.timeout:g}s.")
        print("The capture notes this family only answers a probe sent FROM port 4002,")
        print("which this command does — so a timeout here usually means LAN Control is off.")
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
        print("\nSTEP 2 — Checking I can control the panel.")
        print("Watch it now — it should turn on and go bright.\n")
        send({"msg": {"cmd": "turn", "data": {"value": 1}}})
        time.sleep(0.5)
        send({"msg": {"cmd": "brightness", "data": {"value": 100}}})
        time.sleep(1.5)

        if not _ask("Did the panel turn on / get brighter?", default=True):
            print("\nSo basic control is not getting through. Fix that first:")
            print("  Govee Home app -> your panel -> settings -> turn ON 'LAN Control'")
            print("  Also check this computer is on the same Wi-Fi as the panel.")
            print("\nThen run this again.")
            return 1
        print("\n  Good — basic control works. Now the real test.\n")

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
    listener = _bind(LISTEN_PORT, timeout)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    found: Dict[str, Dict[str, Any]] = {}
    try:
        sender.sendto(json.dumps(SCAN_REQUEST).encode(), (MULTICAST_ADDR, MULTICAST_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload, origin = listener.recvfrom(4096)
            except socket.timeout:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            data = (message.get("msg") or {}).get("data") or {}
            found[str(data.get("ip") or origin[0])] = data
    finally:
        listener.close()
        sender.close()

    if not found:
        print("  I could not find any Govee device on this network.\n")
        print("  Almost always one of these two things:\n")
        print("   1. LAN Control is off.")
        print("      Govee Home app -> your panel -> settings -> turn ON 'LAN Control'")
        print("   2. This computer is on a different Wi-Fi than the panel.")
        print("      (Guest networks and 5GHz-vs-2.4GHz splits both cause this.)\n")
        print("  Fix that, then run this again.")
        return ""

    if len(found) == 1:
        ip = next(iter(found))
        print(f"  Found it: {found[ip].get('sku', 'device')} at {ip}")
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
