"""Helpers around the ``ffmpeg`` / ``ffprobe`` command line tools.

Commands are always run as argument lists (never through a shell), so Windows paths and
Unicode file names are passed through untouched.
"""

from __future__ import annotations

import json
import re
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


def probe_video_size(path: str | Path) -> tuple[int, int]:
    """``(width, height)`` in pixels of the first video stream, as displayed (rotation applied)."""
    data = _probe(
        path,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
    )  # fmt: skip
    try:
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FFmpegError(f"Could not read the picture size of {path}") from exc
    rotation = stream.get("tags", {}).get("rotate")
    for side in stream.get("side_data_list", []) or []:
        rotation = side.get("rotation", rotation)
    try:
        if rotation is not None and abs(int(float(rotation))) % 180 == 90:
            width, height = height, width
    except (TypeError, ValueError):
        pass
    return width, height


def has_audio_stream(path: str | Path) -> bool:
    """True if the file contains at least one audio stream."""
    data = _probe(path, "-select_streams", "a", "-show_entries", "stream=index")
    return bool(data.get("streams"))


# A long filter graph is passed as a file, because Windows limits a command line to about 32,000
# characters. The option for that was renamed: ffmpeg up to 7 has `-filter_complex_script <file>`,
# ffmpeg 8 removed it in favour of `-/filter_complex <file>`. The ffmpeg version is read first to
# pick the likely spelling; if ffmpeg still refuses it, the other spelling is tried, and as a last
# resort the graph text is passed inline with `-filter_complex` (fine for graphs under ~30,000
# characters). The spelling that worked is remembered for the next call.
_FILTER_FILE_OPTIONS = ("-filter_complex_script", "-/filter_complex")
_working_filter_option: int | None = None
_UNSUPPORTED_MARKERS = (
    "unrecognized option",
    "option not found",
    "error splitting the argument",
    "filter_complex_script",
    "/filter_complex",
)
_MAX_INLINE_GRAPH = 28000


def parse_ffmpeg_major(version_output: str) -> int | None:
    """Major version from ``ffmpeg -version`` output (``ffmpeg version 8.0-full_build...``)."""
    match = re.search(r"ffmpeg version\s+[nN]?(\d+)(?:\.(\d+))?", version_output)
    return int(match.group(1)) if match else None


def _detect_filter_option() -> int:
    """Index in ``_FILTER_FILE_OPTIONS`` expected to work with the installed ffmpeg."""
    try:
        out = subprocess.run(
            [find_ffmpeg(), "-version"], capture_output=True, check=False, timeout=30
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, FFmpegError):
        return 0
    major = parse_ffmpeg_major(out)
    return 1 if major is not None and major >= 8 else 0


def run_ffmpeg_filter_file(
    before: Sequence[str | Path],
    filter_file: str | Path,
    after: Sequence[str | Path],
    *,
    desc: str = "",
) -> None:
    """Run ffmpeg with the complex filter graph read from ``filter_file``.

    The command is ``ffmpeg ... <before> <filter-file option> <file> <after>``. Works with every
    ffmpeg version: if an option is not known it tries the other spelling, then the inline form.
    """
    global _working_filter_option
    first = (
        _working_filter_option if _working_filter_option is not None else _detect_filter_option()
    )
    attempts: list[tuple[int | None, str, str | Path]] = [
        (first, _FILTER_FILE_OPTIONS[first], filter_file),
        (1 - first, _FILTER_FILE_OPTIONS[1 - first], filter_file),
    ]
    try:
        graph = Path(filter_file).read_text(encoding="utf-8")
    except OSError:
        graph = ""
    if graph and len(graph) <= _MAX_INLINE_GRAPH:
        attempts.append((None, "-filter_complex", graph))
    for position, (index, option, value) in enumerate(attempts):
        try:
            run_ffmpeg([*before, option, value, *after], desc=desc)
        except FFmpegError as exc:
            unsupported = any(m in str(exc).lower() for m in _UNSUPPORTED_MARKERS)
            if unsupported and position < len(attempts) - 1:
                continue
            raise
        if index is not None:
            _working_filter_option = index
        return
