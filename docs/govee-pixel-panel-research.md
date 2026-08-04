# Govee Pixel Panel — finding the undocumented per-pixel path

Govee's public API stops at power, brightness, one colour, and a handful of
zones. A 52×32 H6631 has 1,664 individually addressable LEDs and the Govee app
drives every one of them, so **the capability exists on the wire** — it is
simply not written down.

This document is the plan for finding it, and `tools/govee_pixel_probe.py` is
the instrument. The driver (`lumisync/drivers/govee_pixel.py`) is already
written against four candidate encodings; hardware picks the winner.

---

## What is already confirmed

From this repo's own LAN capture
(`docs/govee-lan-research-and-implementation.md`) plus LumiSync's working strip
control:

| Fact | Evidence |
|---|---|
| Control is plain JSON over UDP to port **4003** | Runtime pcap |
| Status probes must be sent **from source port 4002** | A probe from an ephemeral port timed out; the same probe from 4002 answered immediately |
| Discovery is multicast to **239.255.255.250:4001**, replies to 4002 | Govee Desktop constants |
| Segment colour rides in a base64 `pt` payload | `{"msg":{"cmd":"razer","data":{"pt":"…"}}}` |
| The frame is `BB <len16> <op> <body…> <xor>` | `convert_colors()`, working against real strips |
| Razer/DreamView mode toggle is `BB 00 01 B1 <on> <xor>` | `switch_razer()`, working |
| Segment colour opcode is `0xB0` | `convert_colors()` |

The driver reproduces the mode frame **byte-for-byte** (`BB 00 01 B1 01 0A`) and
there is a test pinning it, because that is the one piece of this protocol
already proven against hardware.

## The actual problem, stated precisely

Look at the confirmed segment frame:

```
BB  len_hi len_lo  B0  01  count  r g b  r g b  …  xor
    └─ 16 bit ─┘           └ 8 bit ┘
```

Two fields, two different widths, and the difference is the whole puzzle:

- **The length field is 16-bit.** It can describe 4,992 bytes of pixel data
  without any change. *The frame format is not the limit.*
- **The count field is 8-bit.** One frame cannot name more than 255 segments.
  *That* is the limit, and it is why LumiSync caps at 255 today.

A 1,664-pixel panel therefore cannot be driven by the strip encoding as-is. The
firmware must be doing one of a small number of things, and each is testable.

## The four candidates

All four are implemented in `lumisync/drivers/govee_pixel.py` as pure functions,
unit-tested, and selectable by name. Sizes are for a full 52×32 frame:

| Strategy | Idea | Packets | Max packet | Why it might be right |
|---|---|---|---|---|
| `single` | One frame, count written as **16-bit** | 1 | 5,000 B | Tests whether the firmware reads the frame's own length rather than the count byte |
| `chunked` | Frames carrying a **start offset** + ≤255 pixels | 7 | 774 B | What essentially every long-strip protocol converges on; widening the *address* while leaving count a byte |
| `rows` | One frame per panel **row**, tagged by index | 32 | 164 B | Chunking on a boundary a 2D panel's firmware likely already knows |
| `groups` | One packet per **distinct colour** + coordinate list | 1–12 | ~1 KB | Exactly how the iDotMatrix panels in this repo draw. Enormously cheaper for pixel art |

`groups` deserves a note: on a real dashboard frame — mostly black with a dozen
flat colours — it encodes the whole panel in **13 bytes** versus 5,000. If it
works it is not just viable, it is the right answer for this use case.

`single` is the one to be careful with: 5,000 bytes exceeds the ~1,500-byte MTU,
so it will IP-fragment. Embedded network stacks routinely drop fragments. If it
fails, that failure does not cleanly distinguish "wrong encoding" from "dropped
fragments" — so treat a `single` failure as weak evidence and rely on the others.

## The procedure

Run these where the hardware is, on your own network.

### 1. Confirm the panel answers

```bash
python tools/govee_pixel_probe.py scan
python tools/govee_pixel_probe.py status --ip <panel-ip>
```

Nothing back? Turn on **LAN Control** in Govee Home → the device → settings.
That is the single most common reason this comes up empty, and nothing below
works without it.

### 2. Watch the real app — the step that actually answers the question

```bash
python tools/govee_pixel_probe.py watch --save capture.jsonl
```

Leave it running and drive the panel from the Govee app: open a DIY scene,
change a pixel, start DreamView. Every packet is logged and every `pt` payload
decoded to opcode, length, body and checksum.

If nothing appears, the app is going via the cloud rather than the LAN for that
feature. That is itself a finding — note it and move to step 4.

### 3. Diff two captures — the highest-signal move

```bash
# capture a solid colour, change exactly ONE pixel in the app, capture again
python tools/govee_pixel_probe.py diff before.jsonl after.jsonl
```

The bytes that move between those two captures **are** the pixel-addressing
format. One pixel changed, so whatever shifted encodes position and colour. This
one comparison usually settles the entire question.

### 4. Replay the candidates

```bash
python tools/govee_pixel_probe.py try --ip <panel-ip>
```

Sends a test pattern with each encoding in turn, three seconds apart. The
pattern has four distinct corners (red / green / blue / white) and a centre
cross, so a transposed grid, a mirrored axis, or a wrong row stride is obvious
rather than subtle.

Watch the panel and note which run drew it.

## What to send back

Any one of these finishes it:

- **Which strategy drew the pattern** in step 4, and whether it was oriented
  correctly, or
- **The `diff` output** from step 3, or
- **The `capture.jsonl`** from step 2 — the raw frames are enough to work from.

Then `DEFAULT_STRATEGY` becomes that encoding, `STRATEGY_VERIFIED` flips to
`True`, and Pixel Dash streams live frames to the panel. Everything above the
driver already works: the sink probes for `draw_grid()`, finds it, and resolves
to per-pixel mode.

## Until then

The driver reports itself as `UNVERIFIED` and nothing in Pixel Dash claims the
panel is showing a real dashboard. The honest fallbacks stay in place:

- **The GIF** — `dashboard.gif` at exactly 52×32, importable as a DIY scene.
  Full resolution, manual.
- **Scene switching** — `tools/govee_scene_bridge.py`, automatic but limited to
  pictures made in advance.

Neither is as good as streaming. That is the point of this document.

## Boundaries

This is interoperability work on hardware you own, on your own LAN, using the
device's own local control interface — the same interface LumiSync already uses
for strips. It reads what the panel and its app exchange and replays candidate
commands to that panel.

It does not touch Govee's cloud, does not use or need an account, does not
bypass authentication, and does not redistribute any Govee code or asset. If a
capture reveals the encoding, what gets written down here is a byte layout — the
same category of fact as the `0xB0` frame this repo already documents.
