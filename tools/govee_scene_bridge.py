r"""Drive a Govee pixel panel from a rendered Pixel Dash manifest.

This lives in ``tools/`` on purpose. LumiSync's runtime is deliberately
cloud-free — "direct LAN communication, no cloud required" is a promise the
package keeps and a test enforces — so anything that talks to a vendor cloud
stays out of ``lumisync/`` and runs as a separate, opt-in process.

    Pixel Dash  ->  dashboard.json  ->  this bridge  ->  Govee cloud API

Read this before wiring it up, because what it can and cannot do is the point.

**It cannot upload the dashboard.** Govee's public v2 API exposes power,
brightness, ``colorRgb``, ``segmentedColorRgb`` and scene *selection*. There is
no documented capability for sending arbitrary per-pixel data or an image, so
no amount of code here will push a rendered 52x32 frame to the panel.

**It can switch between pictures you already made.** DIY scenes are created in
the Govee Home app — including by importing a GIF or PNG — and each gets an id.
This bridge activates one of those in response to what the dashboard currently
shows. The workflow:

1. Render the states you care about (``python -m lumisync.pixeldash once``
   writes ``dashboard.gif``; run it in the situations you want covered).
2. Import each into Govee Home as a DIY scene — once, by hand.
3. Map each state to its scene id via the environment variables below.
4. Run this bridge with ``--watch``. The panel then switches itself: green day,
   red day, a win, a loss, a dead feed.

That is a real automation, and it is honestly the ceiling of what the public API
allows today. With no scene mapping the bridge degrades to a status light — one
colour derived from the current state — and says so.

Setup::

    # Govee Home app -> Profile -> Settings -> Apply for API Key (arrives by email)
    export GOVEE_API_KEY=...
    python tools/govee_scene_bridge.py --devices        # find your sku + device id
    export GOVEE_DEVICE_SKU=H6631
    export GOVEE_DEVICE_ID=AB:CD:...
    export GOVEE_SCENE_GREEN=12 GOVEE_SCENE_RED=13 GOVEE_SCENE_WIN=14

    python tools/govee_scene_bridge.py --watch --manifest ~/pixeldash/dashboard.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

API_HOST = "https://openapi.api.govee.com"
CONTROL_PATH = "/router/api/v1/device/control"
DEVICES_PATH = "/router/api/v1/user/devices"
#: Not part of the documented reference. Tried once, best-effort; a failure is
#: not an error, because scene ids can always be entered by hand.
DIY_SCENES_PATH = "/router/api/v1/device/diy-scenes"

GOVEE_VARS = {
    "api_key": "GOVEE_API_KEY",
    "device": "GOVEE_DEVICE_ID",
    "sku": "GOVEE_DEVICE_SKU",
}

#: Capability coordinates, from Govee's v2 control reference.
CAP_BRIGHTNESS = ("devices.capabilities.range", "brightness")
CAP_COLOR = ("devices.capabilities.color_setting", "colorRgb")
CAP_DIY_SCENE = ("devices.capabilities.dynamic_scene", "diyScene")

#: Dashboard states a scene can be mapped to.
STATES = ("win", "loss", "green", "red", "flat", "no_trades", "feed_down", "setup")

STATE_COLORS: Dict[str, Tuple[int, int, int]] = {
    "win": (46, 230, 118),
    "loss": (255, 62, 72),
    "green": (46, 230, 118),
    "red": (255, 62, 72),
    "flat": (120, 124, 140),
    "no_trades": (70, 74, 88),
    "feed_down": (255, 176, 32),
    "setup": (56, 214, 255),
}


def rgb_to_int(color: Tuple[int, int, int]) -> int:
    """Pack an RGB tuple into the single integer the API expects."""
    r, g, b = (max(0, min(255, int(channel))) for channel in color)
    return (r << 16) | (g << 8) | b


def state_from_manifest(manifest: Dict[str, Any]) -> str:
    """Which mapped state this render represents.

    Events win over the rotation: a fill that just printed is what the panel
    should show, not the daily summary it was on a second ago.
    """
    for event in manifest.get("events") or []:
        kind = str(event.get("kind", ""))
        if kind == "win":
            return "win"
        if kind == "loss":
            return "loss"
        if kind == "feed_down":
            return "feed_down"

    scenes = manifest.get("scenes") or []
    if "setup" in scenes:
        return "setup"
    if "error" in scenes:
        return "feed_down"

    headline = str((manifest.get("plan") or {}).get("headline", "")).upper()
    if "GREEN" in headline:
        return "green"
    if "RED" in headline:
        return "red"
    if "NO TRADES" in headline:
        return "no_trades"
    return "flat"


@dataclass
class BridgeConfig:
    api_key: str = ""
    device: str = ""
    sku: str = ""
    scenes: Dict[str, int] = field(default_factory=dict)
    brightness: int = 70
    timeout: float = 8.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.device and self.sku)

    @property
    def missing(self) -> Tuple[str, ...]:
        return tuple(
            var
            for var, value in (
                (GOVEE_VARS["api_key"], self.api_key),
                (GOVEE_VARS["device"], self.device),
                (GOVEE_VARS["sku"], self.sku),
            )
            if not value
        )

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "BridgeConfig":
        source = environ if environ is not None else os.environ

        scenes: Dict[str, int] = {}
        for state in STATES:
            raw = str(source.get(f"GOVEE_SCENE_{state.upper()}", "") or "").strip()
            if raw.isdigit():
                scenes[state] = int(raw)

        return cls(
            api_key=str(source.get(GOVEE_VARS["api_key"], "") or "").strip(),
            device=str(source.get(GOVEE_VARS["device"], "") or "").strip(),
            sku=str(source.get(GOVEE_VARS["sku"], "") or "").strip(),
            scenes=scenes,
        )


class GoveeBridge:
    """Talks to the Govee cloud API on behalf of a rendered dashboard."""

    def __init__(self, config: BridgeConfig, *, opener=None) -> None:
        self.config = config
        self._opener = opener
        self._last_command: Optional[Tuple[str, Any]] = None
        self._brightness_sent = False
        self._request_id = 0

    # --- transport ---
    def _open(self, request):
        fetch = self._opener or urllib.request.urlopen
        return fetch(request, timeout=self.config.timeout)

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{API_HOST}{path}", headers={"Govee-API-Key": self.config.api_key}
        )
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _control(self, capability: Tuple[str, str], value: Any) -> Any:
        self._request_id += 1
        kind, instance = capability
        body = {
            "requestId": f"pixeldash-{self._request_id}",
            "payload": {
                "sku": self.config.sku,
                "device": self.config.device,
                "capability": {"type": kind, "instance": instance, "value": value},
            },
        }
        request = urllib.request.Request(
            f"{API_HOST}{CONTROL_PATH}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Govee-API-Key": self.config.api_key,
            },
            method="POST",
        )
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    # --- discovery ---
    def list_devices(self) -> List[Dict[str, Any]]:
        payload = self._get(DEVICES_PATH)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    def discover_scenes(self) -> Dict[str, int]:
        """Best-effort DIY scene lookup, keyed by upper-cased name.

        This endpoint is not in Govee's published reference, so it is tried once
        and any failure returns nothing rather than raising.
        """
        try:
            payload = self._get(
                f"{DIY_SCENES_PATH}?sku={self.config.sku}&device={self.config.device}"
            )
        except Exception:
            return {}

        scenes: Dict[str, int] = {}
        for entry in _walk_scene_entries(payload):
            name = str(entry.get("name", "") or entry.get("sceneName", "")).strip().upper()
            value = entry.get("value", entry.get("id"))
            if name and isinstance(value, int):
                scenes[name] = value
        return scenes

    # --- applying ---
    def apply(self, state: str) -> str:
        """Push ``state`` to the panel; returns a one-line description."""
        if not self.config.configured:
            return f"not configured — missing {', '.join(self.config.missing)}"

        self._apply_brightness()
        scene_id = self.config.scenes.get(state)
        if scene_id is not None:
            sent = self._send_once(CAP_DIY_SCENE, int(scene_id))
            return f"{state} -> DIY scene {scene_id}" + ("" if sent else " (unchanged)")

        color = STATE_COLORS.get(state, (255, 255, 255))
        sent = self._send_once(CAP_COLOR, rgb_to_int(color))
        return (
            f"{state} -> colour {color}"
            + ("" if sent else " (unchanged)")
            + f"; no DIY scene mapped for '{state}'"
        )

    def _apply_brightness(self) -> None:
        if self._brightness_sent:
            return
        try:
            self._control(CAP_BRIGHTNESS, max(1, min(100, int(self.config.brightness))))
            self._brightness_sent = True
        except Exception:
            # Brightness is a nicety; losing it must not block the state change.
            pass

    def _send_once(self, capability: Tuple[str, str], value: Any) -> bool:
        """Send only when the command differs from the last one.

        Govee rate-limits to 12 requests per second per account. Re-sending an
        identical scene every tick spends that budget for no visible change.
        """
        key = (capability[1], value)
        if self._last_command == key:
            return False
        self._control(capability, value)
        self._last_command = key
        return True


def _walk_scene_entries(payload: Any) -> List[Dict[str, Any]]:
    """Pull scene-shaped dicts out of whatever the endpoint returned.

    Written defensively because the response shape is unverified: anything with
    a name and an integer value is a candidate, and nothing is assumed about the
    envelope around it.
    """
    found: List[Dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if ("name" in node or "sceneName" in node) and (
                isinstance(node.get("value"), int) or isinstance(node.get("id"), int)
            ):
                found.append(node)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(payload)
    return found


def read_manifest(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def default_manifest_path() -> str:
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.join(base, "LumiSync", "pixeldash", "dashboard.json")


def command_devices(bridge: GoveeBridge) -> int:
    try:
        devices = bridge.list_devices()
    except urllib.error.HTTPError as exc:
        hint = " — check GOVEE_API_KEY" if exc.code in (401, 403) else ""
        print(f"Could not list devices: HTTP {exc.code}{hint}")
        return 1
    except Exception as exc:
        print(f"Could not list devices: {type(exc).__name__}: {exc}")
        return 1

    if not devices:
        print("The API key works but the account has no devices on it.")
        return 1

    print("Devices on this account:\n")
    for device in devices:
        sku = device.get("sku", "?")
        identifier = device.get("device", "?")
        name = device.get("deviceName", "")
        print(f"  {sku:<8} {identifier}  {name}")
    print("\nSet the one you want:")
    print(f"  export {GOVEE_VARS['sku']}=<sku>")
    print(f"  export {GOVEE_VARS['device']}=<device id>")

    if bridge.config.configured:
        scenes = bridge.discover_scenes()
        if scenes:
            print("\nDIY scenes found:\n")
            for name, value in sorted(scenes.items()):
                print(f"  {value:<8} {name}")
        else:
            print(
                "\nNo DIY scenes discovered — that endpoint is not in Govee's published\n"
                "reference, so it may simply not be readable. Read the ids from the\n"
                "Govee Home app and set GOVEE_SCENE_* by hand."
            )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="govee_scene_bridge",
        description="Switch Govee DIY scenes from a rendered Pixel Dash manifest.",
    )
    parser.add_argument("--manifest", default="", help="path to dashboard.json")
    parser.add_argument("--devices", action="store_true", help="list devices and DIY scenes")
    parser.add_argument("--watch", action="store_true", help="follow the manifest as it updates")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between checks")
    parser.add_argument("--brightness", type=int, default=70, help="panel brightness, 1-100")
    args = parser.parse_args(argv)

    config = BridgeConfig.from_env()
    config.brightness = args.brightness

    if not config.api_key:
        print(f"No {GOVEE_VARS['api_key']} set.\n")
        print("Govee Home app -> Profile -> Settings -> Apply for API Key.")
        print("The key arrives by email, usually within a few minutes.")
        return 1

    bridge = GoveeBridge(config)
    if args.devices:
        return command_devices(bridge)

    if not config.configured:
        print(f"Missing {', '.join(config.missing)} — run with --devices to find them.")
        return 1

    manifest_path = args.manifest or default_manifest_path()
    seen_digest = ""

    while True:
        manifest = read_manifest(manifest_path)
        if manifest is None:
            print(f"No manifest at {manifest_path} — is Pixel Dash running?")
            if not args.watch:
                return 1
        else:
            digest = str(manifest.get("digest", ""))
            if digest != seen_digest:
                seen_digest = digest
                state = state_from_manifest(manifest)
                try:
                    print(bridge.apply(state))
                except urllib.error.HTTPError as exc:
                    hint = " — check GOVEE_API_KEY" if exc.code in (401, 403) else ""
                    print(f"control failed: HTTP {exc.code}{hint}")
                except Exception as exc:
                    print(f"control failed: {type(exc).__name__}: {exc}")

        if not args.watch:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
