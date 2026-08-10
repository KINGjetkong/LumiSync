"""Discover feed specs on disk.

Specs live in a ``feeds.d`` directory next to the rendered output. Adding an API
is copying a JSON file in; removing one is deleting it. Load errors are
collected rather than raised, so one malformed spec cannot stop the other feeds
from starting — it comes back as an error status and gets drawn on the panel.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from .declarative import FeedSpec, SpecError, parse_spec

FEEDS_DIRNAME = "feeds.d"
EXAMPLE_FILENAME = "example.json.sample"

#: Written into a fresh ``feeds.d`` so the format is discoverable without
#: leaving the app. The ``.sample`` suffix keeps the loader from picking it up.
EXAMPLE_SPEC: Dict = {
    "name": "example",
    "base_url": "https://api.example.com",
    "data_class": "paper",
    "account_label": "EXAMPLE",
    "auth": {"type": "bearer", "token_env": "EXAMPLE_API_TOKEN"},
    "positions": {
        "path": "/v1/positions",
        "records": "data.positions",
        "fields": {
            "symbol": "ticker",
            "quantity": "qty",
            "entry_price": "avg_price",
            "mark_price": "mark",
        },
    },
    "trades": {
        "path": "/v1/trades",
        "records": "data.trades",
        "params": {"start": "{since}", "end": "{until}"},
        "mode": "closed",
        "fields": {"symbol": "ticker", "realized": "pnl", "closed_at": "closed_at"},
    },
    "equity": {"path": "/v1/account", "value": "data.equity"},
}


def feeds_dir(output_dir: str) -> str:
    return os.path.join(output_dir, FEEDS_DIRNAME)


def ensure_feeds_dir(output_dir: str) -> str:
    """Create ``feeds.d`` and drop the annotated example in it if it is new."""
    directory = feeds_dir(output_dir)
    try:
        os.makedirs(directory, exist_ok=True)
        example = os.path.join(directory, EXAMPLE_FILENAME)
        if not os.path.exists(example):
            with open(example, "w", encoding="utf-8") as handle:
                json.dump(EXAMPLE_SPEC, handle, indent=2)
    except OSError:
        # A read-only output directory is survivable: specs just cannot be
        # added there. Everything else keeps working.
        pass
    return directory


def load_specs(directory: str) -> Tuple[List[FeedSpec], List[Tuple[str, str]]]:
    """Load every ``*.json`` spec in ``directory``.

    Returns the specs that parsed and ``(path, message)`` for those that did
    not. Files are read in sorted order so the feed list is stable between
    runs.
    """
    specs: List[FeedSpec] = []
    errors: List[Tuple[str, str]] = []

    try:
        names = sorted(
            name for name in os.listdir(directory) if name.lower().endswith(".json")
        )
    except OSError:
        return specs, errors

    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            errors.append((path, f"unreadable: {exc}"))
            continue

        try:
            specs.append(parse_spec(raw, source_path=path))
        except SpecError as exc:
            errors.append((path, str(exc)))

    return specs, _reject_duplicates(specs, errors)


def _reject_duplicates(
    specs: List[FeedSpec], errors: List[Tuple[str, str]]
) -> List[Tuple[str, str]]:
    """Drop specs whose name collides with an earlier one.

    Two feeds with the same name would produce two feed statuses the diffing
    logic cannot tell apart, so the later one is refused loudly instead.
    """
    seen: Dict[str, str] = {}
    for spec in list(specs):
        if spec.name in seen:
            specs.remove(spec)
            errors.append(
                (spec.source_path, f"duplicate feed name {spec.name!r} (already in {seen[spec.name]})")
            )
        else:
            seen[spec.name] = spec.source_path
    return errors


def describe(spec: FeedSpec) -> str:
    """One-line summary for the CLI's ``check`` output."""
    parts = [spec.base_url, spec.data_class.value]
    if spec.positions:
        parts.append("positions")
    if spec.trades:
        parts.append(f"trades:{spec.trades.mode}")
    if spec.equity:
        parts.append("equity")
    if spec.token_env:
        parts.append(f"${spec.token_env}")
    return " · ".join(parts)


def load_spec_file(path: str) -> Optional[FeedSpec]:
    """Load one spec, for tests and one-off checks."""
    with open(path, "r", encoding="utf-8") as handle:
        return parse_spec(json.load(handle), source_path=path)
