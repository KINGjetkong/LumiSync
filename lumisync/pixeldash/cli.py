"""Headless entry point: ``python -m lumisync.pixeldash``.

Useful on a machine with no desktop session — a VPS running the trading stack
can render the dashboard GIF on a timer and let something else display it.
Everything except the on-screen sinks works without Qt.
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import List, Optional, Sequence

from .config import PixelDashConfig, load_config, setup_instructions
from .models import KNOWN_TARGETS
from .service import PixelDashService, ServiceTick
from .sinks.base import Sink
from .sinks.files import FileSink


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lumisync-pixeldash",
        description="Render a live trading dashboard as pixel art.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "once", "check", "screens"),
        help=(
            "run: poll and render on a loop · once: a single render · "
            "check: report configuration and feed health · "
            "screens: list displays the ghost sink can target"
        ),
    )
    parser.add_argument("--env-file", default="", help="read credentials from a KEY=value file")
    parser.add_argument("--output", default="", help="directory for the GIF, still and manifest")
    parser.add_argument(
        "--target",
        default="",
        choices=("",) + tuple(KNOWN_TARGETS),
        help="panel geometry to compose for (default H6631, 52x32)",
    )
    parser.add_argument("--interval", type=float, default=0.0, help="seconds between refreshes")
    parser.add_argument("--frame-ms", type=int, default=0, help="milliseconds per rendered frame")
    parser.add_argument("--scene-seconds", type=float, default=0.0, help="seconds per scene")
    parser.add_argument("--days", type=int, default=0, help="calendar window in days")
    parser.add_argument("--scale", type=int, default=1, help="upscale factor for the written GIF")
    parser.add_argument("--history", action="store_true", help="also keep a digest-named copy")
    parser.add_argument("--ticks", type=int, default=0, help="stop after N refreshes (0 = forever)")
    parser.add_argument("--no-planner", action="store_true", help="skip the scene planner cache")
    return parser


def config_from_args(args: argparse.Namespace) -> PixelDashConfig:
    overrides = {}
    if args.output:
        overrides["output_dir"] = args.output
    if args.interval:
        overrides["refresh_seconds"] = args.interval
    if args.frame_ms:
        overrides["frame_ms"] = args.frame_ms
    if args.scene_seconds:
        overrides["scene_seconds"] = args.scene_seconds
    if args.days:
        overrides["calendar_days"] = args.days
    if args.no_planner:
        overrides["planner_enabled"] = False

    config = load_config(env_file=args.env_file, **overrides)
    if args.target:
        config.target = KNOWN_TARGETS[args.target]
    return config


def command_check(config: PixelDashConfig) -> int:
    """Report what is configured and whether each feed answers."""
    from .feeds.registry import build_feeds

    print(f"target      : {config.target.name} ({config.target.cols}x{config.target.rows})")
    print(f"output      : {config.output_dir}")
    print(f"refresh     : {config.refresh_seconds:g}s · scenes {config.scene_seconds:g}s")
    print(f"planner     : {'on' if config.planner_enabled else 'off'} -> {config.plan_cache_dir}")

    feeds, statuses = build_feeds(config)
    for status in statuses:
        print(f"feed {status.name:<10}: {status.state.value.upper()} — {status.detail}")

    for feed in feeds:
        try:
            positions = feed.fetch_positions()
            print(
                f"feed {feed.name:<10}: OK — {feed.account_label()} "
                f"[{feed.data_class.value}] {len(positions)} open"
            )
        except Exception as exc:
            print(f"feed {feed.name:<10}: ERROR — {type(exc).__name__}: {exc}")

    notes = setup_instructions(config)
    if notes:
        print("\nstill needed:")
        for note in notes:
            print(f"  - {note}")
        return 1
    return 0 if feeds else 1


def command_screens() -> int:
    from .sinks.surface import describe_screens, qt_available

    if not qt_available():
        print("PySide6 is not available; the ghost and hover sinks need it.")
        return 1

    from PySide6.QtWidgets import QApplication

    owns = QApplication.instance() is None
    app = QApplication(sys.argv[:1]) if owns else QApplication.instance()
    try:
        lines = describe_screens()
        if not lines:
            print("no screens detected")
            return 1
        for line in lines:
            print(line)

        from .sinks.surface import find_virtual_screen

        virtual = find_virtual_screen()
        print(
            f"\nghost target: {virtual.name()} ({virtual.model()})"
            if virtual is not None
            else "\nghost target: none — no Virtual Display Driver monitor found"
        )
        return 0
    finally:
        if owns:
            app.quit()


def report(tick: ServiceTick) -> None:
    print(tick.summary())
    for event in tick.events:
        print(f"  ! {event.kind.value}: {event.title} {event.detail}".rstrip())
    for status in tick.snapshot.failed_feeds:
        print(f"  x {status.name}: {status.detail}")
    for entry in tick.reports:
        print(f"  -> {entry.summary}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    if args.command == "screens":
        return command_screens()
    if args.command == "check":
        return command_check(config)

    sinks: List[Sink] = [
        FileSink(config.output_dir, scale=max(1, args.scale), keep_history=args.history)
    ]
    service = PixelDashService(config, sinks=sinks)

    try:
        if args.command == "once":
            tick = service.refresh()
            report(tick)
            return 0 if tick.ok else 1

        print(f"polling every {config.refresh_seconds:g}s — ctrl-c to stop")
        stop = threading.Event()
        try:
            service.run_forever(on_tick=report, stop=stop, max_ticks=args.ticks)
        except KeyboardInterrupt:
            stop.set()
            print("stopped")
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
