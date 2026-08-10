"""Trade-journal notes, stored beside the rendered frames.

The numbers on a journal day always come from the broker. This module only owns
the part the broker cannot know: what the trader wrote about the day. Notes are
merged onto :class:`DayStats` at read time and never influence a P&L figure.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Dict, Iterable, List, Optional, Tuple

from .models import DayStats, JournalNote

JOURNAL_FILENAME = "journal.json"


class Journal:
    """A date-keyed note store backed by a single JSON file.

    Reads are tolerant — a corrupt or missing file yields an empty journal
    rather than an exception, because losing notes must never stop the dash
    from showing live P&L.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._notes: Dict[_dt.date, JournalNote] = {}
        self.load()

    # --- persistence ---
    def load(self) -> None:
        self._notes = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return

        for key, value in raw.items():
            date = _parse_date(key)
            if date is None:
                continue
            if isinstance(value, str):
                self._notes[date] = JournalNote(date=date, text=value)
            elif isinstance(value, dict):
                tags = value.get("tags")
                self._notes[date] = JournalNote(
                    date=date,
                    text=str(value.get("text", "")),
                    tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
                )

    def save(self) -> None:
        """Write the journal atomically so a crash mid-write cannot truncate it."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            date.isoformat(): {"text": note.text, "tags": note.tags}
            for date, note in sorted(self._notes.items())
        }
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.path)

    # --- access ---
    def get(self, date: _dt.date) -> Optional[JournalNote]:
        return self._notes.get(date)

    def set(self, date: _dt.date, text: str, tags: Optional[List[str]] = None) -> None:
        text = text.strip()
        if not text and not tags:
            self._notes.pop(date, None)
        else:
            self._notes[date] = JournalNote(date=date, text=text, tags=list(tags or []))
        self.save()

    def tags(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for note in self._notes.values():
            for tag in note.tags:
                if tag not in seen:
                    seen.append(tag)
        return tuple(sorted(seen))

    def annotate(self, days: Iterable[DayStats]) -> Tuple[DayStats, ...]:
        """Attach note text to each day, leaving every number untouched."""
        from dataclasses import replace

        result = []
        for day in days:
            note = self._notes.get(day.date)
            result.append(replace(day, note=note.text) if note else day)
        return tuple(result)


def default_journal_path(output_dir: str) -> str:
    return os.path.join(output_dir, JOURNAL_FILENAME)


def _parse_date(value: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(value))
    except ValueError:
        return None
