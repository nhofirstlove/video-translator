"""Persistent monthly character counter for ElevenLabs (free tier: a few thousand per month).

The counter lives in ``<project data dir>/usage/elevenlabs_YYYY-MM.json`` so that it survives
between jobs and program restarts; a new month starts a new file (counter back to zero).
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from video_translator.paths import resolve_path


def usage_file(
    data_dir: str | Path, service: str = "elevenlabs", today: date | None = None
) -> Path:
    """Path of this month's counter file inside ``data_dir/usage``."""
    today = today or date.today()
    return Path(data_dir) / "usage" / f"{service}_{today:%Y-%m}.json"


def default_usage_path(work_root: str | Path = "data/work", today: date | None = None) -> Path:
    """Counter file next to the work folder (``data/work`` -> ``data/usage/...``)."""
    return usage_file(resolve_path(work_root).parent, today=today)


class UsageTracker:
    """Counts characters spent this month against ``monthly_budget``."""

    def __init__(self, path: str | Path, monthly_budget: int) -> None:
        self.path = Path(path)
        self.monthly_budget = max(0, int(monthly_budget))
        self.used = self._load()

    def _load(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return max(0, int(data.get("used", 0)))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    @property
    def remaining(self) -> int:
        return max(0, self.monthly_budget - self.used)

    def can_spend(self, n: int) -> bool:
        return n <= self.remaining

    def record(self, n: int) -> None:
        """Add ``n`` characters and save at once (atomic write)."""
        self.used += max(0, int(n))
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"used": self.used}), encoding="utf-8")
        os.replace(tmp, self.path)
