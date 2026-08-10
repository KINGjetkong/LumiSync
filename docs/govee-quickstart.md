# Get the dashboard on your Govee panel — the short version

> **Want an agent on your Mac to do it instead?**
> [AGENT_PROMPT.md](AGENT_PROMPT.md) is a copy-paste prompt for Claude Code (or
> any coding agent) running on the machine that can reach the panel.

Five steps. About ten minutes. You don't need to understand any of it.

If you want to know *why* any of this is happening, that's
[govee-pixel-panel-research.md](govee-pixel-panel-research.md). You don't need
it to do this.

---

## Step 1 — Turn on LAN Control

**On your phone.** This is the one step everything else depends on.

1. Open the **Govee Home** app
2. Tap your pixel panel
3. Go into its **settings** (gear icon, usually top-right)
4. Find **LAN Control** and turn it **ON**

Without this the panel ignores your computer completely.

---

## Step 2 — Put the computer on the same Wi-Fi as the panel

Same network. Not a guest network.

If your router splits 2.4GHz and 5GHz into two names, the panel is almost
certainly on the 2.4GHz one — put the computer there too.

---

## Step 3 — Get the code

Open a terminal (Mac: **Terminal**. Windows: **PowerShell**) and paste:

```bash
git clone https://github.com/KINGjetkong/LumiSync.git
cd LumiSync
git checkout claude/trading-dashboard-workflow-5cxt2g
pip install -e .
```

If `pip` isn't found, try `pip3`. If `git` isn't found, install it from
[git-scm.com](https://git-scm.com/downloads).

---

## Step 4 — Run the one command

**Fastest option — no questions at all:**

```bash
python tools/govee_pixel_probe.py report --ip 192.168.4.68
```

Runs every check by itself and prints one block. Copy the whole thing, send it
to me, done. Watch the panel while it runs and tell me if any colour appeared.

**Or the guided version**, which asks you what you saw:

```bash
python tools/govee_pixel_probe.py wizard
```

That's it. That's the whole thing.

It will find your panel, flash some colours on it, and ask you simple
questions like *"Did you see RED?"* — type `y` or `n` and press Enter.

**Have the panel where you can see it.** That's the only real requirement.

It takes about two minutes and ends by printing something like:

```
  WORKING METHOD: chunked
```

---

## Step 5 — Send me that line

Copy whatever it printed at the end and send it to me.

Three things can happen, and all three are fine:

| What it says | What it means | What you send |
|---|---|---|
| `WORKING METHOD: <something>` | 🎉 Found it | That line |
| `Showed colour: …` but scrambled | Close — data's landing, order's wrong | That line |
| `NONE OF THEM WORKED` | Panel uses a different format | Follow its instructions, send `capture.jsonl` |

Once I have it, the panel streams your live dashboard.

---

## If the panel ignores commands

If the wizard says the panel answers but **ignores** commands, or you saw no
colours at all, the panel speaks a different dialect than Govee's strips. One
capture settles it.

**Important:** driving the panel from your **phone** won't work here — that
traffic never touches your Mac, so nothing can record it. Use the **Govee
desktop app on this computer**, or capture on whichever machine runs the app.

```bash
python tools/govee_pixel_probe.py sniff
```

It asks for your password (recording network traffic needs it), then:

1. While it runs, open the Govee **desktop** app and show a picture on the panel
2. Press **Ctrl-C**
3. It prints what it found — send me that, or send `govee.pcap`

Already have a Wireshark capture? Skip straight to:

```bash
python tools/govee_pixel_probe.py parse yourfile.pcap
```

---

## If it goes wrong

**"command not found: python"** → try `python3` instead of `python`.

**"I could not find any Govee device"** → in order: (1) VPN on — turn it off,
it swallows discovery; (2) macOS blocking local network — System Settings →
Privacy & Security → **Local Network** → enable Terminal; (3) LAN Control still
off; (4) different Wi-Fi.

Or skip discovery entirely — find the panel's IP in your router and run
`python tools/govee_pixel_probe.py wizard --ip 192.168.4.68`

**Panel ignores commands** → see the section above. Also worth trying: set the
panel to a plain solid colour in the Govee app first (a running scene can
override outside control), and force-quit the Govee app.

**Panel is somewhere you can't see it** → move it, or run the wizard with the
panel's address so you can walk to it:
`python tools/govee_pixel_probe.py wizard --ip 192.168.1.50`

---

## Meanwhile — this already works today

You do **not** have to wait for any of the above to get the dashboard on the
panel. This works right now:

```bash
python -m lumisync.pixeldash once
```

That writes `dashboard.gif` at exactly 52×32 — your panel's real resolution.
Import it in Govee Home as a DIY scene and the dashboard is on the wall.

It's manual, which is why Steps 1–5 exist. But it works today.
