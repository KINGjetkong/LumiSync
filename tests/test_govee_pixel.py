"""Govee pixel-panel driver: frame construction, encodings, and decoding.

Every test here runs without hardware, because every encoder is a pure
function. What hardware decides is *which* encoding the firmware obeys — that
is what ``tools/govee_pixel_probe.py`` is for, and none of these tests claims
to answer it.
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import unittest

from lumisync.drivers.govee_pixel import (
    FRAME_MAGIC,
    MAX_SEGMENTS_PER_FRAME,
    OP_SEGMENT,
    SAFE_PAYLOAD_BYTES,
    STRATEGIES,
    GoveePixelAdapter,
    build_frame,
    build_mode_frame,
    clamp_rgb,
    control_message,
    decode_frame,
    decode_pt,
    encode_chunked,
    encode_grid,
    encode_groups,
    encode_pt,
    encode_rows,
    encode_single,
    xor_checksum,
    _parse_size,
)
from lumisync.drivers.registry import GOVEE_PIXEL_SKUS, create_adapter, is_govee_pixel_panel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

PANEL_COLS, PANEL_ROWS = 52, 32


def solid(cols: int, rows: int, color=(0, 0, 0)):
    return [[color for _ in range(cols)] for _ in range(rows)]


def panel(color=(0, 0, 0)):
    return solid(PANEL_COLS, PANEL_ROWS, color)


class FrameTests(unittest.TestCase):
    def test_checksum_is_an_xor_over_the_frame(self):
        self.assertEqual(xor_checksum([0x01, 0x02, 0x03]), 0x00)
        self.assertEqual(xor_checksum([0xBB, 0x01]), 0xBA)

    def test_a_frame_carries_magic_length_opcode_and_checksum(self):
        frame = build_frame(OP_SEGMENT, [0x01, 0x02])

        self.assertEqual(frame[0], FRAME_MAGIC)
        self.assertEqual(frame[3], OP_SEGMENT)
        self.assertEqual(frame[-1], xor_checksum(frame[:-1]))

    def test_the_length_field_counts_the_body_only(self):
        # Pinned against both confirmed frames: the mode toggle declares 1 for
        # a one-byte body, and convert_colors declares 2 + n*3 for a body of
        # [flag, count, ...rgb]. Including the opcode here would put a wrong
        # length on every packet.
        frame = build_frame(OP_SEGMENT, [0] * 10)
        length = (frame[1] << 8) | frame[2]
        self.assertEqual(length, 10)

    def test_the_length_field_is_sixteen_bit(self):
        # This is the load-bearing fact: the frame format itself can describe a
        # full 52x32 panel, so the 255-segment ceiling is the count byte's,
        # not the frame's.
        frame = build_frame(OP_SEGMENT, [0] * 5000)
        length = (frame[1] << 8) | frame[2]
        self.assertEqual(length, 5000)
        self.assertGreater(length, MAX_SEGMENTS_PER_FRAME)

    def test_the_mode_frame_matches_the_confirmed_bytes(self):
        # Byte-for-byte identical to LumiSync's working switch_razer, which is
        # the one part of this protocol already proven against real hardware.
        self.assertEqual(build_mode_frame(True), bytes([0xBB, 0x00, 0x01, 0xB1, 0x01, 0x0A]))
        self.assertEqual(build_mode_frame(False), bytes([0xBB, 0x00, 0x01, 0xB1, 0x00, 0x0B]))

    def test_frames_round_trip_through_base64(self):
        frame = build_frame(OP_SEGMENT, [1, 2, 3])
        decoded = decode_pt(encode_pt(frame))

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.opcode, OP_SEGMENT)
        self.assertTrue(decoded.checksum_ok)
        self.assertTrue(decoded.length_ok)

    def test_the_control_envelope_matches_the_captured_shape(self):
        message = control_message(build_mode_frame(True))
        self.assertEqual(message["msg"]["cmd"], "razer")
        self.assertEqual(
            base64.b64decode(message["msg"]["data"]["pt"]), build_mode_frame(True)
        )

    def test_colours_are_clamped(self):
        self.assertEqual(clamp_rgb((-5, 300, 128)), (0, 255, 128))


class DecodeTests(unittest.TestCase):
    def test_non_frames_decode_to_none(self):
        self.assertIsNone(decode_frame(b""))
        self.assertIsNone(decode_frame(b"\x01\x02\x03\x04\x05"))
        self.assertIsNone(decode_pt("not base64 at all !!"))

    def test_a_corrupt_checksum_is_reported_not_rejected(self):
        # When reverse-engineering, a frame that fails validation is the
        # interesting one. Discarding it would throw away the finding.
        frame = bytearray(build_frame(OP_SEGMENT, [1, 2, 3]))
        frame[-1] ^= 0xFF
        decoded = decode_frame(bytes(frame))

        self.assertIsNotNone(decoded)
        self.assertFalse(decoded.checksum_ok)
        self.assertIn("BAD-XOR", decoded.summary)

    def test_a_wrong_length_is_reported_not_rejected(self):
        frame = bytearray(build_frame(OP_SEGMENT, [1, 2, 3]))
        frame[2] = 0x7F
        decoded = decode_frame(bytes(frame))

        self.assertIsNotNone(decoded)
        self.assertFalse(decoded.length_ok)
        self.assertIn("BAD-LEN", decoded.summary)

    def test_the_confirmed_captured_payloads_validate(self):
        # Ground truth. "uwABsgAI" is the real off-state pt from this repo's LAN
        # capture; "uwABsQEK" is the razer-on frame LumiSync already sends
        # successfully. Both must decode clean — if either reports BAD-LEN or
        # BAD-XOR, this module's frame layout is wrong, not the capture.
        for payload in ("uwABsgAI", "uwABsQEK"):
            decoded = decode_pt(payload)
            self.assertIsNotNone(decoded, payload)
            self.assertEqual(decoded.magic, FRAME_MAGIC, payload)
            self.assertTrue(decoded.checksum_ok, f"{payload} checksum")
            self.assertTrue(decoded.length_ok, f"{payload} length")

    def test_our_own_frames_agree_with_the_captured_layout(self):
        self.assertEqual(encode_pt(build_mode_frame(True)), "uwABsQEK")


class StrategyTests(unittest.TestCase):
    def test_every_strategy_produces_valid_frames_for_a_full_panel(self):
        grid = panel((10, 20, 30))
        for name in STRATEGIES:
            frames = encode_grid(grid, name)
            self.assertTrue(frames, name)
            for frame in frames:
                decoded = decode_frame(frame)
                self.assertIsNotNone(decoded, name)
                self.assertTrue(decoded.checksum_ok, f"{name} checksum")
                self.assertTrue(decoded.length_ok, f"{name} length")

    def test_every_strategy_is_deterministic(self):
        # The renderer upstream guarantees byte-identical output for the same
        # snapshot; the wire encoding must not break that.
        grid = panel((7, 8, 9))
        grid[3][4] = (255, 0, 0)
        for name in STRATEGIES:
            self.assertEqual(encode_grid(grid, name), encode_grid(grid, name), name)

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(ValueError):
            encode_grid(panel(), "telepathy")

    def test_single_carries_every_pixel_in_one_frame(self):
        frames = encode_single(panel((1, 2, 3)))
        self.assertEqual(len(frames), 1)
        # 4 header + sub-op + 16-bit count + 3 bytes per pixel + checksum
        self.assertEqual(len(frames[0]), 4 + 3 + PANEL_COLS * PANEL_ROWS * 3 + 1)

    def test_single_writes_the_count_as_sixteen_bit(self):
        decoded = decode_frame(encode_single(panel())[0])
        count = (decoded.body[1] << 8) | decoded.body[2]
        self.assertEqual(count, PANEL_COLS * PANEL_ROWS)
        self.assertGreater(count, MAX_SEGMENTS_PER_FRAME)

    def test_chunked_stays_under_the_fragmentation_threshold(self):
        for frame in encode_chunked(panel((5, 5, 5))):
            self.assertLessEqual(len(frame), SAFE_PAYLOAD_BYTES + 16)

    def test_chunked_offsets_are_contiguous_and_cover_the_panel(self):
        frames = encode_chunked(panel((5, 5, 5)))
        covered = 0
        for frame in frames:
            body = decode_frame(frame).body
            offset = (body[1] << 8) | body[2]
            count = body[3]
            self.assertEqual(offset, covered)
            covered += count
        self.assertEqual(covered, PANEL_COLS * PANEL_ROWS)

    def test_chunked_respects_an_explicit_chunk_size(self):
        frames = encode_chunked(panel(), chunk=64)
        expected = -(-PANEL_COLS * PANEL_ROWS // 64)
        self.assertEqual(len(frames), expected)

    def test_chunked_never_overflows_the_eight_bit_count(self):
        # The regression this whole strategy exists to avoid: a chunk larger
        # than the count byte silently wraps and the offsets stop lining up.
        for requested in (0, 64, 255, 397, 5000):
            for frame in encode_chunked(panel(), chunk=requested):
                body = decode_frame(frame).body
                self.assertLessEqual(body[3], MAX_SEGMENTS_PER_FRAME)
                self.assertEqual(body[3], (len(body) - 4) // 3)

    def test_rows_emits_one_frame_per_row_tagged_with_its_index(self):
        frames = encode_rows(panel((9, 9, 9)))
        self.assertEqual(len(frames), PANEL_ROWS)
        for index, frame in enumerate(frames):
            body = decode_frame(frame).body
            self.assertEqual(body[1], index)
            self.assertEqual(body[2], PANEL_COLS)

    def test_rows_frames_each_fit_one_datagram(self):
        for frame in encode_rows(panel()):
            self.assertLess(len(frame), 1500)

    def test_groups_skips_black_so_pixel_art_is_cheap(self):
        grid = panel()          # all black
        grid[0][0] = (255, 0, 0)
        frames = encode_groups(grid)

        self.assertEqual(len(frames), 1)
        self.assertLess(len(frames[0]), 32)

    def test_groups_encodes_one_packet_per_colour(self):
        grid = panel()
        grid[0][0] = (255, 0, 0)
        grid[1][1] = (0, 255, 0)
        grid[2][2] = (0, 0, 255)
        self.assertEqual(len(encode_groups(grid)), 3)

    def test_groups_carries_coordinates_not_a_pixel_run(self):
        grid = solid(4, 4)
        grid[2][3] = (255, 0, 0)
        body = decode_frame(encode_groups(grid)[0]).body

        self.assertEqual(tuple(body[1:4]), (255, 0, 0))
        count = (body[4] << 8) | body[5]
        self.assertEqual(count, 1)
        self.assertEqual((body[6], body[7]), (3, 2))  # x, y

    def test_groups_can_include_black_when_asked(self):
        self.assertEqual(encode_groups(solid(2, 2), skip_black=True), [])
        self.assertEqual(len(encode_groups(solid(2, 2), skip_black=False)), 1)

    def test_strategies_disagree_which_is_the_whole_point(self):
        # If two encodings produced the same bytes there would be nothing to
        # distinguish on hardware.
        grid = panel((1, 2, 3))
        encodings = {name: encode_grid(grid, name) for name in STRATEGIES}
        self.assertEqual(len({tuple(frames) for frames in encodings.values()}), len(STRATEGIES))


class AdapterTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, payload, address):
            self.sent.append((json.loads(payload.decode()), address))

        def close(self):
            pass

    def adapter(self, **overrides):
        sock = self.FakeSocket()
        device = {"ip": "10.0.0.5", "matrix_size": "52x32", "pace_ms": 0, **overrides}
        return GoveePixelAdapter(device, sock), sock

    def test_capabilities_describe_a_matrix(self):
        adapter, _ = self.adapter()
        capabilities = adapter.capabilities

        self.assertTrue(capabilities.is_matrix)
        self.assertEqual(capabilities.matrix_size, (52, 32))
        self.assertEqual(capabilities.segment_count, 52 * 32)

    def test_the_adapter_reports_its_encoding_as_unverified(self):
        # Nothing downstream may imply the panel is confirmed working.
        adapter, _ = self.adapter()
        self.assertIn("UNVERIFIED", adapter.describe())

    def test_draw_grid_sends_razer_messages_to_the_control_port(self):
        adapter, sock = self.adapter()
        adapter.draw_grid(panel((1, 2, 3)))

        self.assertTrue(sock.sent)
        message, address = sock.sent[0]
        self.assertEqual(address, ("10.0.0.5", 4003))
        self.assertEqual(message["msg"]["cmd"], "razer")

    def test_begin_and_end_stream_toggle_the_mode_once(self):
        adapter, sock = self.adapter()
        adapter.begin_stream()
        adapter.begin_stream()
        self.assertEqual(len(sock.sent), 1)

        adapter.end_stream()
        self.assertEqual(len(sock.sent), 2)
        self.assertEqual(
            base64.b64decode(sock.sent[1][0]["msg"]["data"]["pt"]), build_mode_frame(False)
        )

    def test_power_brightness_and_colour_use_the_confirmed_commands(self):
        adapter, sock = self.adapter()
        adapter.set_power(True)
        adapter.set_brightness(140)
        adapter.set_color(300, -2, 20)

        commands = [message["msg"]["cmd"] for message, _ in sock.sent]
        self.assertEqual(commands, ["turn", "brightness", "colorwc"])
        self.assertEqual(sock.sent[1][0]["msg"]["data"]["value"], 100)
        self.assertEqual(
            sock.sent[2][0]["msg"]["data"]["color"], {"r": 255, "g": 0, "b": 20}
        )

    def test_a_missing_ip_is_refused(self):
        adapter, _ = self.adapter(ip="")
        with self.assertRaises(RuntimeError):
            adapter.set_power(True)

    def test_the_strategy_can_be_switched_and_is_validated(self):
        adapter, _ = self.adapter()
        adapter.set_strategy("rows")
        self.assertEqual(adapter.strategy, "rows")
        with self.assertRaises(ValueError):
            adapter.set_strategy("nope")

    def test_matrix_size_parsing(self):
        self.assertEqual(_parse_size("52x32", (1, 1)), (52, 32))
        self.assertEqual(_parse_size("16 X 16", (1, 1)), (16, 16))
        self.assertEqual(_parse_size(None, (52, 32)), (52, 32))
        self.assertEqual(_parse_size("garbage", (52, 32)), (52, 32))


class RegistryTests(unittest.TestCase):
    def test_a_pixel_sku_gets_the_pixel_driver(self):
        adapter = create_adapter({"model": "H6631", "ip": "10.0.0.5"})
        self.assertIsInstance(adapter, GoveePixelAdapter)
        self.assertEqual(adapter.capabilities.matrix_size, (52, 32))
        adapter.close()

    def test_an_explicit_type_also_selects_it(self):
        adapter = create_adapter({"type": "govee_pixel", "ip": "10.0.0.5"})
        self.assertIsInstance(adapter, GoveePixelAdapter)
        adapter.close()

    def test_an_ordinary_strip_still_gets_the_lan_driver(self):
        from lumisync.drivers.govee_lan import GoveeLanAdapter

        adapter = create_adapter({"model": "H619C", "ip": "10.0.0.6"})
        self.assertIsInstance(adapter, GoveeLanAdapter)
        adapter.close()

    def test_sku_detection_is_case_insensitive(self):
        self.assertTrue(is_govee_pixel_panel({"model": "h6631"}))
        self.assertFalse(is_govee_pixel_panel({"model": "H619C"}))

    def test_the_pixel_sku_table_carries_grid_sizes(self):
        for sku, size in GOVEE_PIXEL_SKUS.items():
            self.assertRegex(size, r"^\d+x\d+$", sku)


class MatrixSinkIntegrationTests(unittest.TestCase):
    def test_the_pixel_driver_probes_as_per_pixel(self):
        # The sink resolves to PIXEL mode purely on the driver exposing
        # draw_grid() plus a matrix capability — this is that contract.
        from lumisync.pixeldash.sinks.matrix import PanelMode, probe

        adapter = GoveePixelAdapter({"ip": "10.0.0.5", "matrix_size": "52x32"})
        try:
            capability = probe(adapter)
            self.assertIs(capability.mode, PanelMode.PIXEL)
            self.assertTrue(capability.full_resolution)
            self.assertEqual(capability.matrix_size, (52, 32))
        finally:
            adapter.close()


if __name__ == "__main__":
    unittest.main()


class PcapParsingTests(unittest.TestCase):
    """Reading a capture is the only way to see what the Govee app sends.

    A UDP bind observes only packets addressed to this machine, so it can never
    show another process's outbound traffic — which is exactly the traffic that
    answers the protocol question.
    """

    @staticmethod
    def _pcap(payload: bytes, link_type: int = 1) -> bytes:
        import struct

        udp_length = 8 + len(payload)
        udp = struct.pack(">HHHH", 54321, 4003, udp_length, 0) + payload
        ip = struct.pack(
            ">BBHHHBBH4s4s",
            0x45, 0, 20 + udp_length, 1, 0, 64, 17, 0,
            bytes((192, 168, 4, 10)), bytes((192, 168, 4, 68)),
        )
        link = b"\x00" * 12 + struct.pack(">H", 0x0800) if link_type == 1 else b""
        frame = link + ip + udp

        header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link_type)
        record = struct.pack("<IIII", 0, 0, len(frame), len(frame))
        return header + record + frame

    def _write(self, blob: bytes) -> str:
        import tempfile

        handle = tempfile.NamedTemporaryFile(suffix=".pcap", delete=False)
        handle.write(blob)
        handle.close()
        self.addCleanup(lambda: __import__("os").unlink(handle.name))
        return handle.name

    def test_a_govee_command_is_recovered_from_a_capture(self):
        from govee_pixel_probe import parse_pcap

        payload = json.dumps(control_message(build_mode_frame(True))).encode()
        packets = parse_pcap(self._write(self._pcap(payload)))

        self.assertEqual(len(packets), 1)
        source, destination, recovered = packets[0]
        self.assertEqual(source, "192.168.4.10:54321")
        self.assertEqual(destination, "192.168.4.68:4003")
        self.assertEqual(json.loads(recovered.decode())["msg"]["cmd"], "razer")

    def test_the_frame_inside_a_captured_packet_decodes(self):
        from govee_pixel_probe import parse_pcap

        payload = json.dumps(control_message(build_mode_frame(True))).encode()
        _source, _destination, recovered = parse_pcap(self._write(self._pcap(payload)))[0]

        pt = json.loads(recovered.decode())["msg"]["data"]["pt"]
        frame = decode_pt(pt)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.checksum_ok)
        self.assertTrue(frame.length_ok)

    def test_raw_ip_link_type_is_handled(self):
        from govee_pixel_probe import parse_pcap

        payload = json.dumps({"msg": {"cmd": "status", "data": {}}}).encode()
        packets = parse_pcap(self._write(self._pcap(payload, link_type=101)))
        self.assertEqual(len(packets), 1)

    def test_a_non_pcap_file_is_refused_with_a_useful_message(self):
        from govee_pixel_probe import parse_pcap

        with self.assertRaises(ValueError) as caught:
            parse_pcap(self._write(b"this is not a pcap at all"))
        self.assertIn("tcpdump", str(caught.exception))

    def test_non_udp_traffic_is_skipped(self):
        from govee_pixel_probe import parse_pcap

        import struct

        # A TCP packet — protocol 6 — must not be reported as a UDP payload.
        ip = struct.pack(
            ">BBHHHBBH4s4s", 0x45, 0, 40, 1, 0, 64, 6, 0,
            bytes((10, 0, 0, 1)), bytes((10, 0, 0, 2)),
        )
        frame = b"\x00" * 12 + struct.pack(">H", 0x0800) + ip + b"\x00" * 20
        blob = (
            struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
            + struct.pack("<IIII", 0, 0, len(frame), len(frame))
            + frame
        )
        self.assertEqual(parse_pcap(self._write(blob)), [])
