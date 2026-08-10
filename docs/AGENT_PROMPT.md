# Prompt for an agent running on the Mac

Copy everything inside the box and paste it into Claude Code (or any coding
agent) **running on the Mac that is on the same Wi-Fi as the panel**.

---

```
You are working on a Govee pixel-panel reverse-engineering task. You are running
on my Mac, which is on the same Wi-Fi as the hardware. That matters: the panel
only accepts commands from a host on its own LAN, so this work can only happen
here.

## The hardware

- Govee Gaming Pixel Light, model H6631
- 52 x 32 pixels (1,664 addressable LEDs)
- Current LAN address: 192.168.4.68 (re-discover it if that has changed)
- "LAN Control" is already enabled in the Govee Home app
- Any VPN must be OFF — a VPN swallows LAN discovery entirely

## The goal

Govee documents only power, brightness, one colour, and a handful of zones.
The Govee app clearly drives all 1,664 LEDs, so a per-pixel path exists on the
wire and is simply undocumented. Find it, then make it the default.

Success = a live trading dashboard streams to the panel, full resolution.

## Setup

git clone https://github.com/KINGjetkong/LumiSync.git
cd LumiSync
git checkout claude/trading-dashboard-workflow-5cxt2g
pip install -e .

Read these first, in this order:
  docs/govee-pixel-panel-research.md   <- the problem and the four candidates
  lumisync/drivers/govee_pixel.py      <- the driver and the encoders
  tools/govee_pixel_probe.py           <- the instrument

## What is already known (do not re-derive)

Confirmed from a real capture in docs/govee-lan-research-and-implementation.md:

- Control is plain JSON over UDP to port 4003
- Status probes must be sent FROM source port 4002, or the device ignores them
- Discovery is multicast to 239.255.255.250:4001, replies arrive on 4002
- Segment colour rides in a base64 "pt" payload inside
  {"msg":{"cmd":"razer","data":{"pt":"..."}}}
- The frame is:  BB <len16> <opcode> <body...> <xor-checksum>
- len counts the BODY ONLY - not the opcode, not itself, not the checksum
- Razer/DreamView mode toggle is BB 00 01 B1 <on> <xor>, confirmed working
- Segment colour opcode is 0xB0

The crux: in the known segment frame the LENGTH field is 16-bit (so the frame
format can already describe all 4,992 bytes of pixel data) but the COUNT field
is 8-bit (so one frame cannot name more than 255 segments). A 1,664-pixel panel
must therefore use a wider count, a chunk offset, or many smaller packets.

Four candidate encodings are already implemented and unit-tested in
lumisync/drivers/govee_pixel.py: "single", "chunked", "rows", "groups".
None is confirmed against hardware. That is your job.

## Step 1 - run the automatic diagnostic

python tools/govee_pixel_probe.py report --ip 192.168.4.68

It prints interfaces, discovery, a status read, a MEASURED control test (it
blinks the panel and reads onOff/brightness back rather than asking anyone what
they saw), then fires each encoding and re-reads device state after each.

Ask me to watch the panel while it runs and tell you if any colour appeared —
that is the one signal you cannot read programmatically.

## Step 2 - branch on what you find

If an encoding made the panel show colour:
  Confirm it draws correctly, not just that something lit up:
    python tools/govee_pixel_probe.py try --ip 192.168.4.68 --strategy <name>
  That draws a pattern with four distinct corners (red top-left, green
  top-right, blue bottom-left, white bottom-right) and an orange centre dot.
  Ask me whether the corners are in the right places. If they are mirrored,
  transposed, or offset, fix the pixel ordering in that encoder — the fix is in
  the encode_* function, and the tests in tests/test_govee_pixel.py must still
  pass.

If NOTHING worked, capture what the real app sends:
  python tools/govee_pixel_probe.py sniff
  (needs sudo; it runs tcpdump and then decodes the capture)

  CRITICAL: driving the panel from a PHONE produces nothing capturable on this
  Mac — that traffic never touches this machine. Use a desktop Govee app on
  this machine, or capture on the machine running the app.

  Highest-signal experiment: capture a solid colour, change exactly ONE pixel
  in the app, capture again, then:
    python tools/govee_pixel_probe.py diff before.jsonl after.jsonl
  The bytes that move between those two captures ARE the pixel-addressing
  format. Implement it as a new encoder in STRATEGIES and test it.

## Step 3 - lock it in

Once an encoding is confirmed on hardware, in lumisync/drivers/govee_pixel.py:
  - set DEFAULT_STRATEGY to that encoding
  - set STRATEGY_VERIFIED = True
  - record what you observed in docs/govee-pixel-panel-research.md, including
    anything that did NOT work and why that was informative

Then prove the whole path end to end:
  python -m lumisync.pixeldash once
That renders the dashboard at 52x32. Push it to the panel through the driver
and confirm it is readable on the hardware.

## Rules

- Real observations only. If something is unconfirmed, say so and leave
  STRATEGY_VERIFIED = False. Never mark something verified you did not see work.
- Run the tests before and after any change:  python -m pytest tests/ -q
  All 540 must pass. Do not weaken a test to make a change fit.
- This is my own hardware on my own LAN, using the device's local control
  interface. Do not touch Govee's cloud and do not use my Govee account.
- Report back with: which encoding worked, whether orientation was correct, and
  the exact frame bytes if you captured them.
- If you get stuck, the fastest unblock is the sniff + diff capture. Do that
  before guessing at more encodings.

Start with Step 1 and keep going until the dashboard is on the panel or you
hit something only I can resolve.
```

---

## After the agent finishes

Send me whichever of these it produced:

- the working encoding name, or
- the `diff` / `sniff` output, or
- the `govee-report.txt` block

If it got all the way to a picture on the panel, it can commit and push to the
same branch and I'll pick it up from there.
