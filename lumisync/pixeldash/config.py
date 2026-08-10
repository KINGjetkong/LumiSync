"""Configuration and credential resolution for Pixel Dash.

Credentials come from the environment (or a ``.env``-style file the user points
at), never from source. Nothing here has a usable default: an unset broker means
the feed reports ``UNCONFIGURED`` and the panel shows a setup card. That is the
fail-closed behaviour — a dashboard that renders numbers without a configured
broker would be rendering fiction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import DEFAULT_SCREEN_TARGET, DataClass, RenderTarget, KNOWN_TARGETS

#: Environment variables read for each broker. Documented here so the settings
#: UI and the CLI can print exactly what the user needs to set.
TRADIER_VARS = {
    "token": "TRADIER_ACCESS_TOKEN",
    "account": "TRADIER_ACCOUNT_ID",
    "environment": "TRADIER_ENV",  # "live" (default) or "sandbox"/"paper"
}
ALPACA_VARS = {
    "key_id": "ALPACA_API_KEY_ID",
    "secret": "ALPACA_API_SECRET_KEY",
    "base_url": "ALPACA_BASE_URL",  # defaults to the paper endpoint
}

TRADIER_LIVE_HOST = "https://api.tradier.com"
TRADIER_SANDBOX_HOST = "https://sandbox.tradier.com"
ALPACA_PAPER_HOST = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class BrokerConfig:
    """Resolved credentials for one broker, plus what kind of money it is."""

    name: str
    enabled: bool
    data_class: DataClass
    host: str = ""
    token: str = ""
    account: str = ""
    secret: str = ""
    missing: tuple = ()

    @property
    def configured(self) -> bool:
        return self.enabled and not self.missing


@dataclass
class PixelDashConfig:
    """Everything the service needs to run one dashboard."""

    # --- polling ---
    refresh_seconds: float = 30.0
    #: Positions move faster than the closed-trade ledger; the service polls
    #: them on this shorter cycle so an opening fill shows up quickly.
    position_refresh_seconds: float = 10.0
    calendar_days: int = 90

    # --- rendering ---
    target: RenderTarget = field(default_factory=lambda: KNOWN_TARGETS["H6631"])
    frame_ms: int = 120
    #: Scene rotation. Each entry is a scene id from
    #: :mod:`lumisync.pixeldash.render.scenes`.
    rotation: List[str] = field(
        default_factory=lambda: ["daily", "positions", "calendar", "agents"]
    )
    #: Seconds a scene stays up before the rotation advances.
    scene_seconds: float = 8.0
    brightness: int = 70

    # --- output ---
    output_dir: str = ""
    panel_device_id: str = ""
    ghost_enabled: bool = False
    hover_enabled: bool = True
    hover_scale: int = 6
    hover_opacity: float = 0.9
    #: Geometry the on-screen sinks render at. Denser than the panel so the
    #: layout earns the larger font; set to "" to make them match the panel.
    screen_target: str = DEFAULT_SCREEN_TARGET

    #: Directory of declarative API specs. Drop a JSON file in here and the
    #: feed appears alongside the built-in brokers; see
    #: :mod:`lumisync.pixeldash.feeds.specs`.
    feeds_dir: str = ""

    # --- planner ---
    #: Cache directory for AI-authored scene plans. Plans are keyed by a digest
    #: of the snapshot, so a cached plan makes the render fully deterministic
    #: and keeps the model out of the polling loop entirely.
    plan_cache_dir: str = ""
    planner_enabled: bool = True

    brokers: Dict[str, BrokerConfig] = field(default_factory=dict)

    @property
    def configured_brokers(self) -> List[BrokerConfig]:
        return [broker for broker in self.brokers.values() if broker.configured]

    @property
    def data_class(self) -> DataClass:
        """The strictest class present — live money wins the label.

        With no configured broker the class is irrelevant (nothing renders),
        but PAPER is the safe answer because it never overstates the stakes.
        """
        classes = [broker.data_class for broker in self.configured_brokers]
        if DataClass.LIVE in classes:
            return DataClass.LIVE
        return DataClass.PAPER


def _env(name: str, environ: Optional[Dict[str, str]] = None) -> str:
    source = environ if environ is not None else os.environ
    return str(source.get(name, "") or "").strip()


def load_dotenv(path: str) -> Dict[str, str]:
    """Parse a ``KEY=value`` file into a dict without touching ``os.environ``.

    Blank lines, ``#`` comments, ``export`` prefixes and surrounding quotes are
    handled. Unreadable files yield an empty dict — a missing env file is a
    normal state, not an error.
    """
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def resolve_tradier(environ: Optional[Dict[str, str]] = None) -> BrokerConfig:
    """Build the Tradier broker config from the environment.

    ``TRADIER_ENV=sandbox`` (or ``paper``) points at the sandbox host and tags
    every number it returns as paper money.
    """
    token = _env(TRADIER_VARS["token"], environ)
    account = _env(TRADIER_VARS["account"], environ)
    mode = _env(TRADIER_VARS["environment"], environ).lower()

    sandbox = mode in ("sandbox", "paper", "test")
    missing = tuple(
        var
        for var, value in ((TRADIER_VARS["token"], token), (TRADIER_VARS["account"], account))
        if not value
    )
    return BrokerConfig(
        name="tradier",
        enabled=bool(token or account),
        data_class=DataClass.PAPER if sandbox else DataClass.LIVE,
        host=TRADIER_SANDBOX_HOST if sandbox else TRADIER_LIVE_HOST,
        token=token,
        account=account,
        missing=missing,
    )


def resolve_alpaca(environ: Optional[Dict[str, str]] = None) -> BrokerConfig:
    """Build the Alpaca broker config. Defaults to the paper endpoint.

    A live Alpaca host is honoured if the user sets one explicitly, and the
    data class follows the host so the panel never labels live money as paper.
    """
    key_id = _env(ALPACA_VARS["key_id"], environ)
    secret = _env(ALPACA_VARS["secret"], environ)
    base_url = _env(ALPACA_VARS["base_url"], environ) or ALPACA_PAPER_HOST

    missing = tuple(
        var
        for var, value in ((ALPACA_VARS["key_id"], key_id), (ALPACA_VARS["secret"], secret))
        if not value
    )
    is_paper = "paper" in base_url.lower()
    return BrokerConfig(
        name="alpaca",
        enabled=bool(key_id or secret),
        data_class=DataClass.PAPER if is_paper else DataClass.LIVE,
        host=base_url.rstrip("/"),
        token=key_id,
        secret=secret,
        missing=missing,
    )


def load_config(
    environ: Optional[Dict[str, str]] = None,
    *,
    env_file: str = "",
    **overrides,
) -> PixelDashConfig:
    """Assemble a config from the environment plus explicit overrides.

    ``env_file`` values fill gaps in ``environ`` but never override a variable
    that is already set — the shell wins over a checked-in file.
    """
    source: Dict[str, str] = dict(environ if environ is not None else os.environ)
    if env_file:
        for key, value in load_dotenv(env_file).items():
            source.setdefault(key, value)

    brokers = {
        "tradier": resolve_tradier(source),
        "alpaca": resolve_alpaca(source),
    }

    config = PixelDashConfig(brokers=brokers)

    target_name = _env("PIXELDASH_TARGET", source)
    if target_name and target_name in KNOWN_TARGETS:
        config.target = KNOWN_TARGETS[target_name]

    output_dir = _env("PIXELDASH_OUTPUT_DIR", source)
    if output_dir:
        config.output_dir = output_dir

    for key, value in overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)

    if not config.output_dir:
        config.output_dir = default_output_dir()
    if not config.plan_cache_dir:
        config.plan_cache_dir = os.path.join(config.output_dir, "plans")
    if not config.feeds_dir:
        from .feeds.specs import ensure_feeds_dir

        config.feeds_dir = ensure_feeds_dir(config.output_dir)
    return config


def default_output_dir() -> str:
    """Per-user directory for rendered GIFs and cached scene plans."""
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.join(base, "LumiSync", "pixeldash")


def setup_instructions(config: PixelDashConfig) -> List[str]:
    """Human-readable list of what still needs configuring.

    Rendered onto the panel as the setup card, so it has to stay short.
    """
    notes: List[str] = []
    for broker in config.brokers.values():
        if not broker.enabled:
            continue
        for var in broker.missing:
            notes.append(f"set {var}")
    if not any(broker.enabled for broker in config.brokers.values()):
        notes.append(f"set {TRADIER_VARS['token']} + {TRADIER_VARS['account']}")
    return notes
