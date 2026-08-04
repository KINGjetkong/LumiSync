# Pixel Dash — a live trading dashboard as pixel art

Pixel Dash polls your broker, folds the result into a small immutable snapshot,
renders that snapshot as a pixel-art animation, and routes the animation to
wherever you want to see it: a GIF on disk, a hover overlay pinned over your
charts, a full-screen ghost display on a virtual monitor, or a physical LED
panel.

```
broker feed  ->  DashboardSnapshot  ->  ScenePlan  ->  frames  ->  GIF  ->  sinks
```

Everything lives in `lumisync/pixeldash/`. It has no dependencies beyond what
LumiSync already ships (numpy, Pillow, PySide6), and everything except the
on-screen sinks runs headless.

---

## Two rules that shape the whole package

### 1. Real data, or a visible error

No module in `pixeldash` can produce a number that did not come from a broker.
There is no demo mode, no sample data, no placeholder P&L.

When something is missing the code says so structurally rather than defaulting:

| Situation | What happens |
|---|---|
| No broker credentials | The panel renders the **setup card** listing the env vars to set |
| Feed returns HTTP 5xx | The panel renders the **error card** with the feed name and the error |
| Broker returns no mark for a position | That position shows `--`, and the account total is **refused** rather than partially summed |
| A day has no closed trades | The calendar draws it **unlit**, distinct from a flat-P&L day |
| One of two brokers is down | The healthy broker still renders; the failure appears as its own card |

The last row of that table has a subtlety worth stating: if a *live* feed fails
and only a paper feed is answering, the whole snapshot is re-labelled `PAPR`.
A panel stamped LIVE while every number on it came from paper would be worse
than no panel.

### 2. The same snapshot always renders the same bytes

`render(snapshot, config)` is deterministic. Every source of variation — the
sparkles on a win, the scene ordering — is seeded from
`snapshot_digest(snapshot)`, a content hash of the snapshot itself. Nothing in
the render path reads a clock.

That is what makes a frame reproducible: the manifest written next to each GIF
records the digest, so a dashboard from three weeks ago can be regenerated
exactly from its journal entry.

```python
first  = render(snapshot, config)
second = render(snapshot, config)
assert first.gif_bytes() == second.gif_bytes()
```

---

## Data sources

| Broker | Positions | Closed trades | Notes |
|---|---|---|---|
| **Tradier** | `/v1/accounts/{id}/positions` + `/v1/markets/quotes` | `/v1/accounts/{id}/gainloss` | The account of record. Marks are NBBO **midpoints**, not `last` |
| **Alpaca** | `/v2/positions` | `/v2/account/activities/FILL`, FIFO-matched locally | Paper sleeve by default |

Two details are load-bearing:

**Tradier positions carry no price.** Marks come from a separate quotes call,
and an option's mark is the bid/ask midpoint. A stale `last` print on an
illiquid 0DTE strike can misstate open P&L by more than the position is worth.

**Alpaca has no realized gain/loss endpoint.** Round trips are reconstructed
from the fill stream with FIFO matching (`feeds/roundtrip.py`) so cost basis
agrees with how the broker itself reports it. Only fills Alpaca actually
returned are used; unmatched opening fills stay open rather than being closed
at an assumed price.

### Configuration

Credentials come from the environment, never from source:

```bash
# Tradier — the default source
export TRADIER_ACCESS_TOKEN="..."
export TRADIER_ACCOUNT_ID="..."
export TRADIER_ENV="live"        # or "sandbox" / "paper" -> tags everything PAPER

# Alpaca — optional paper sleeve
export ALPACA_API_KEY_ID="..."
export ALPACA_API_SECRET_KEY="..."
export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
```

`--env-file` reads the same keys from a `KEY=value` file. Shell variables win
over file values.

Check what is wired up:

```bash
python -m lumisync.pixeldash check
```

---

## Adding an API without writing code

Beyond the built-in brokers, a feed can be **described** rather than programmed.
Drop a JSON file in `feeds.d` and it appears alongside Tradier and Alpaca — same
snapshot, same scenes, same fail-closed behaviour.

```
<output_dir>/feeds.d/myprop.json
```

```json
{
  "name": "myprop",
  "base_url": "https://api.example.com",
  "data_class": "live",
  "auth": {"type": "bearer", "token_env": "MYPROP_TOKEN"},
  "positions": {
    "path": "/v1/positions",
    "records": "data.rows",
    "fields": {
      "symbol": "ticker",
      "quantity": "qty",
      "entry_price": "avg_price",
      "mark_price": "last"
    }
  },
  "trades": {
    "path": "/v1/fills",
    "records": "data.rows",
    "params": {"start": "{since}", "end": "{until}"},
    "mode": "fills",
    "fields": {
      "symbol": "ticker", "side": "side", "quantity": "qty",
      "price": "px", "filled_at": "ts"
    }
  }
}
```

`python -m lumisync.pixeldash check` lists every spec it found and every spec it
rejected, with the reason.

| Key | Meaning |
|---|---|
| `base_url` | Host the paths hang off |
| `data_class` | `live` / `paper` / `backtest` — drives the panel's chip |
| `auth.type` | `none`, `bearer`, `header`, `query`, `basic` |
| `auth.token_env` | **Name** of the env var holding the credential |
| `records` | Dotted path to the list in the response (`data.rows`) |
| `fields` | Pixel Dash field → response field, dotted paths allowed |
| `params` | Query string; `{since}` and `{until}` are substituted |
| `mode` | `closed` for a realized ledger, `fills` for a fill stream |

Three properties are enforced rather than suggested:

**Credentials never live in the spec.** `token_env` names an environment
variable; the file holds the variable's name, never its value, so a spec is safe
to commit and share. A spec that tries to inline a token is rejected at load.

**A spec cannot invent a number.** Every value is read from the response or it
is `None`. A missing mark renders as `--` and the account total is refused —
exactly as it would from Tradier.

**Validation happens at load, not at poll.** Unknown field names, bad auth
types, missing paths and duplicate feed names are all rejected with a message
naming the file. A typo shows up as a startup error, not as a dashboard that is
quietly missing a feed.

`mode: "fills"` runs the same FIFO matching the Alpaca feed uses, for brokers
that report executions rather than round trips.

## Scenes

The rotation is configurable; scenes with nothing to show drop out
automatically.

| Scene | Shows |
|---|---|
| `daily` | Today's realized P&L, W/L record, win rate, RTH vs ETH split |
| `positions` | Open positions by risk, unrealized %, proportional bars, paged 3 at a time |
| `calendar` | Journal heat grid — weekdays down, weeks across, today ringed |
| `agents` | Workflow agents as pixel avatars, body colour = state, focus stepping through |
| `error` | Which feed is down and why |
| `setup` | Which environment variables are missing |

**Sessions.** Every trade is tagged RTH (09:30–16:00 ET, weekdays) or ETH. The
daily card shows both, and each calendar day keeps its own split, because
averaging the two hides the difference between them.

**The calendar's colour scale is relative.** The largest absolute day in the
window maps to full saturation, so the grid self-scales to whatever size the
account trades.

### Notifications

Transitions between consecutive snapshots become full-panel bursts that
interrupt the rotation:

| Event | Trigger | Panel |
|---|---|---|
| `WIN` | A new closed trade finished green | "ANOTHER WIN" + trophy + amount, with confetti |
| `LOSS` | A new closed trade finished red | "MISSION FAILED" + skull + amount |
| `POSITION_OPENED` / `POSITION_CLOSED` | Position set changed | Symbol and size |
| `FEED_DOWN` / `FEED_RECOVERED` | Feed health flipped | Feed name and error |
| `AGENT_UPDATE` | An agent changed **state** | Agent label and message |

Agent notifications fire on state changes only, never on message changes. Feed
agents report live counts ("2 open / 31 closed") that change on almost every
poll; firing on those would burst the panel every thirty seconds and train you
to ignore the one signal meant to make you look up.

---

## The planner: AI-assisted, still deterministic

The planner is the only place a model can influence output, and it is fenced in
three ways.

**It decides framing, not content.** A *plan* is scene order, headline copy and
an accent colour. Numbers only ever come from the feeds — a plan cannot put a
figure on the panel.

**It is content-addressed.** Plans are cached under a digest of the *situation*
(is the day green, are feeds up, which symbols are open) rather than the exact
numbers. Same situation, same cached plan, byte-identical render. A dollar of
P&L drift does not invalidate a plan; a swing from green to red does.

**It never runs in the polling loop.** At most once per novel situation, off the
hot path — which matches the doctrine of keeping models in scheduler and
selector layers, never in a trade path.

Everything a hook returns is validated: unknown scene ids are dropped, headlines
are clamped and character-filtered, and a near-black accent is rejected. Any
failure — no hook, unreachable model, malformed response, raised exception —
falls back to the deterministic rule table. **The dashboard is fully functional
with no model attached; that is the default.**

```bash
export PIXELDASH_PLANNER_HOOK="mypackage.planner:choose_scenes"
```

```python
def choose_scenes(context: dict) -> dict:
    # context carries no account id, no position sizes, no dollar amounts
    return {"scenes": ["daily", "calendar"], "headline": "GRIND DAY"}
```

---

## Output sinks

### Files — always works

Writes `dashboard.gif`, `dashboard.png` and `dashboard.json` to a stable path,
so anything watching that folder (OBS, a browser source, a wallpaper engine)
can hold one filename forever. `--history` also keeps a digest-named copy.

### Hover overlay

A small always-on-top window pinned to a screen corner, click-through by default
so it never intercepts a mouse event meant for the chart underneath. This is the
surface you glance at mid-trade.

### Ghost display

Full-screens the dashboard on a virtual monitor created by the
[Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver).
It never covers real work, never steals focus, and stays capturable.

The screen is located by EDID identity (`Virtual Display`, `VDD by MTT`,
`MTT1337`, serial `VDD001`), not by index — plugging in a real monitor
renumbers everything. **If no virtual display exists the sink reports that
instead of falling back to the primary screen**; a dashboard that full-screens
itself over your trading platform is a bug with consequences.

The VDD repo has a companion script, `Community Scripts/ghostdisplay-VDD.ps1`,
that sizes the ghost and keeps it off the Windows primary.

```bash
python -m lumisync.pixeldash screens   # list displays and the detected ghost
```

### Physical panel

What reaches the hardware depends on what the hardware can actually do. The sink
probes the driver rather than guessing from a model name:

| Mode | When | Result |
|---|---|---|
| `PIXEL` | Driver exposes a per-pixel surface (iDotMatrix BLE) | The dashboard as drawn |
| `SEGMENTS` | Per-zone colour stream only (Govee Razer/DreamView, ≤255 zones) | Reduced to a strip of averages — a mood, not a readable dashboard |
| `AMBIENT` | Single colour only | A status light: green day, red day, amber when a feed is down |
| `NONE` | No usable colour control | Reports failure |

A sink that was asked for per-pixel and could only manage an average reports
**degraded**, not success.

Frames are de-duplicated before playback. A rotation holds each scene for
seconds at a time, so ~250 rendered frames usually collapse to a handful of
distinct images — the difference between a BLE panel keeping up and falling
over.

---

## Getting at the Govee Gaming Pixel Light (H6631)

The H6631 is a 52×32 Wi-Fi pixel panel, which is why that geometry is the
default render target. Here is exactly what is and is not reachable.

### What each access path gives you

| Path | What you need | What you get |
|---|---|---|
| **Govee Home app** | The app | **Full per-pixel.** Import a GIF or PNG as a DIY scene. Manual, one-off |
| **Cloud API (v2)** | An API key | Power, brightness, one colour, per-zone colour, and **selecting a saved scene by id**. No image upload |
| **LAN API** | "LAN Control" enabled in the app | Power, brightness, one colour. Officially nothing more |
| **BLE** | — | Undocumented; nothing verified for this model |

**There is no documented way to push an arbitrary 52×32 frame to this panel from
code.** Govee's v2 capability list is `powerSwitch`, `brightness`, `colorRgb`,
`colorTemperatureK`, `segmentedColorRgb` (≤15 zones), `lightScene`, `diyScene`,
`snapshot`, `musicMode`. `diyScene` *selects* a scene you already made; it does
not upload one. This package will not claim a pixel path it cannot verify.

### So what actually works

Two things, and together they cover most of what you'd want:

**1. The GIF (full resolution, manual import).** The file sink writes
`dashboard.gif` at exactly 52×32. Import it in Govee Home as a DIY scene and the
real dashboard is on the panel. That is why the file sink is a first-class
output rather than a debug artifact.

**2. Scene switching (automatic, limited to pictures you pre-made).** Make a DIY
scene per state you care about — green day, red day, a win, a loss, a dead feed
— then let the panel switch itself. `tools/govee_scene_bridge.py` watches the
manifest Pixel Dash writes and activates the matching scene id.

### What you need for the automatic path

```bash
# 1. Govee Home app -> Profile -> Settings -> Apply for API Key.
#    It arrives by email, usually within minutes.
export GOVEE_API_KEY="..."

# 2. Find the device. Prints every device on the account.
python tools/govee_scene_bridge.py --devices
export GOVEE_DEVICE_SKU="H6631"
export GOVEE_DEVICE_ID="..."

# 3. Create the DIY scenes in the app (import the rendered GIFs), then map
#    their ids. Any state you don't map falls back to a solid colour.
export GOVEE_SCENE_GREEN=12
export GOVEE_SCENE_RED=13
export GOVEE_SCENE_WIN=14
export GOVEE_SCENE_LOSS=15
export GOVEE_SCENE_FEED_DOWN=16

# 4. Run it alongside Pixel Dash.
python tools/govee_scene_bridge.py --watch
```

Rate limits are 12 requests/second per account and 120/minute per device. The
bridge holds the last command and skips repeats, so a steady state costs nothing.

### Why the bridge lives in `tools/`

LumiSync's runtime is deliberately cloud-free — *"direct LAN communication, no
cloud required"* is a promise the package makes and a test enforces. So anything
that talks to a vendor cloud stays out of `lumisync/` and runs as a separate,
opt-in process that consumes the manifest:

```
Pixel Dash  ->  dashboard.json  ->  govee_scene_bridge  ->  Govee cloud API
```

### Going after the per-pixel path directly

The vendor's ceiling is not the hardware's ceiling. The app drives all 1,664
LEDs over the LAN, so the capability is on the wire and undocumented rather than
absent.

`lumisync/drivers/govee_pixel.py` is a real per-pixel driver written against
four candidate encodings, and `tools/govee_pixel_probe.py` captures what the
Govee app actually sends so the right one can be identified against your panel.
Full procedure: **[docs/govee-pixel-panel-research.md](govee-pixel-panel-research.md)**.

Until a capture confirms an encoding the driver reports itself `UNVERIFIED` and
nothing here claims the panel is showing a live dashboard. Once it is confirmed,
`probe()` already resolves that driver to `PIXEL` mode — no other change needed.

---

## Running it

### In the app

The **Pixel Dash** page has a live preview, output toggles, a target-panel
selector, a device picker and the journal note editor. Polling runs on a worker
thread; publishing happens on the GUI thread because two of the three sinks are
Qt widgets.

### Headless

```bash
python -m lumisync.pixeldash check              # what is configured, what answers
python -m lumisync.pixeldash once               # render one dashboard and exit
python -m lumisync.pixeldash run --interval 30  # poll and re-render on a loop
python -m lumisync.pixeldash screens            # list displays, find the ghost

python -m lumisync.pixeldash run \
    --target H6631 --interval 15 --scale 8 --history \
    --output ~/pixeldash
```

Useful on a VPS running the trading stack: render the GIF on a timer and let
something else display it.

---

## Two sizes, because a screen is not a panel

An LED matrix has a fixed pixel count. A hover overlay or a ghost display can
draw as many pixels as it likes — so the screen surfaces render on a **denser
grid with physically smaller pixels**, which is what buys room for a larger,
much more legible face.

| Target | Grid | Font | Used by |
|---|---|---|---|
| `H6631` | 52×32 | 3×5 | The panel — hardware resolution |
| `screen` | 104×64 | **5×7** | Hover overlay, ghost display (default) |
| `screen-xl` | 156×96 | **5×7** | Larger ghost displays and capture sources |
| `64x32`, `32x32`, `16x32`, `16x16` | — | 3×5 | Other panels |

`screen` and `screen-xl` are exact 2× and 3× multiples of the H6631 grid, so a
layout checked on one is proportionally identical on the others.

The service renders **once per geometry any sink asked for** — typically twice
per tick — and routes each sink to its own. The plan is resolved once and shared
across both, since it describes the situation rather than the geometry.

Nothing in the scene composers is a pixel constant. Layout comes from
`render/metrics.py`, computed from the target: font, header height, line height,
row pitch, calendar cell size, and the scale of the hero number. That is what
lets the same code lay out on a 16×16 panel and on a 156×96 surface.

The font choice is made on **available rows**, not columns: a wide-but-short
target that took the 5×7 face on width alone would have room for the letters and
nowhere to put them.

## Layout notes

**The panel font is mostly 3×5, with three exceptions.** `M`, `N` and `W` are
4–5 pixels wide. A three-pixel `N` either reads as an `S` (draw the diagonal) or
as an `M` (fill the body), and both mistakes land in words this dashboard shows
constantly: OPEN, DOWN, WIN, MISSION. There is a test asserting those three
glyphs stay distinct in both faces.

**The `$` symbol is off by default.** It is not separable from `S` at three
pixels wide, and the sign plus the colour already say what the number is. The
5×7 face does have a real `$` if you want it.

**Headlines wrap rather than squeeze.** "MISSION FAILED" does not fit one
52-pixel row; tightening the letter spacing to force it makes the word
unreadable at exactly the moment it matters most.

**Green is brighter than red on purpose.** At a glance the brightness carries
the sign even when the hue does not.

---

## Module map

```
lumisync/pixeldash/
├── models.py            # Snapshot, Position, Trade, DayStats, sessions
├── config.py            # Credential resolution, panel geometry
├── collector.py         # Poll every feed into one snapshot, fault-isolated
├── stats.py             # Calendar grids, heat scaling, streaks, summary
├── journal.py           # Date-keyed notes (never touches a number)
├── events.py            # Snapshot diffing -> notifications
├── format.py            # Panel-width number formatting
├── service.py           # The poll loop
├── cli.py               # Headless entry point
├── feeds/
│   ├── base.py          # Feed interface + JSON-over-HTTPS client
│   ├── tradier.py       # Positions, gain/loss, balances
│   ├── alpaca.py        # Positions, fill stream
│   ├── declarative.py   # Feeds described by a JSON spec
│   ├── specs.py         # Discover and validate specs in feeds.d
│   ├── roundtrip.py     # FIFO round-trip reconstruction
│   └── registry.py      # Config -> feeds, reporting what failed
├── render/
│   ├── font.py          # 3x5 panel face + 5x7 screen face
│   ├── metrics.py       # Layout measurements derived from the target
│   ├── palette.py       # Colours tuned for a diffused LED panel
│   ├── canvas.py        # Frame buffer + drawing primitives
│   ├── sprites.py       # Trophy, skull, agent avatars, warning, plug
│   ├── scenes.py        # Scene composers
│   ├── planner.py       # Scene selection + plan cache + hook validation
│   ├── gif.py           # Deterministic GIF encoding
│   └── pipeline.py      # snapshot -> digest -> frames -> GIF
└── sinks/
    ├── files.py         # GIF, still and manifest
    ├── matrix.py        # Physical panel, capability-probed
    ├── ghost.py         # Virtual display surface
    ├── hover.py         # Always-on-top HUD
    └── surface.py       # Shared Qt widget + screen discovery

tools/govee_scene_bridge.py  # Optional, out-of-package Govee cloud bridge
```

## Tests

```bash
python -m pytest tests/test_pixeldash_*.py
```

Covers determinism, fail-closed behaviour on every feed failure mode, FIFO
matching (long, short, partial, out-of-order), scene layout on every supported
panel geometry, planner hook validation, and sink capability probing.
