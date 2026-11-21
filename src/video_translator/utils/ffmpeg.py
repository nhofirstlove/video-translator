"""Thin helpers around the ``ffmpeg`` / ``ffprobe`` command line tools (placeholders)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def run_ffmpeg(args: Sequence[str]) -> None:
    """Run ``ffmpeg`` with ``args``; raise on a non-zero exit code."""
    raise NotImplementedError("run_ffmpeg is not implemented yet")


def probe_duration(path: Path) -> float:
    """Return the media duration in seconds (via ``ffprobe``)."""
    raise NotImplementedError("probe_duration is not implemented yet")
