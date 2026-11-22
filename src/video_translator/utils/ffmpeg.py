"""Helpers around the ``ffmpeg`` / ``ffprobe`` command line tools.

Commands are always run as argument lists (never through a shell), so Windows paths and
Unicode file names are passed through untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

STDERR_TAIL_CHARS = 2000


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe is missing or a command failed."""


def _find_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise FFmpegError(
            f"{name} was not found on PATH. Install FFmpeg and reopen the terminal, "
            "then run `video-translator doctor` to check."
        )
    return found


def find_ffmpeg() -> str:
    """Path of the ``ffmpeg`` executable."""
    return _find_tool("ffmpeg")


def find_ffprobe() -> str:
    """Path of the ``ffprobe`` executable."""
    return _find_tool("ffprobe")


def _run(cmd: list[str], desc: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise FFmpegError(f"Could not start {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace").strip()[-STDERR_TAIL_CHARS:]
        what = f" ({desc})" if desc else ""
        name = Path(cmd[0]).name
        raise FFmpegError(f"{name} failed{what} with code {result.returncode}:\n{tail}")
    return result


def run_ffmpeg(args: Sequence[str | Path], *, desc: str = "") -> None:
    """Run ``ffmpeg -y -hide_banner -loglevel error <args>``; raise FFmpegError on failure."""
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *(str(a) for a in args)]
    _run(cmd, desc)


def _probe(path: str | Path, *extra: str) -> dict:
    cmd = [find_ffprobe(), "-v", "error", "-print_format", "json", *extra, str(path)]
    result = _run(cmd, f"probe {Path(path).name}")
    try:
        return json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned invalid output for {path}") from exc


def probe_duration(path: str | Path) -> float:
    """Media duration in seconds (via ffprobe)."""
    data = _probe(path, "-show_entries", "format=duration")
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FFmpegError(f"Could not read the duration of {path}") from exc


def has_audio_stream(path: str | Path) -> bool:
    """True if the file contains at least one audio stream."""
    data = _probe(path, "-select_streams", "a", "-show_entries", "stream=index")
    return bool(data.get("streams"))
