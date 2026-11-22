"""Waveform summaries for the editor timeline: the loudest sample in each short time slice.

``peaks`` are integers 0..100 (loudness of one slice, compressed a little so quiet speech is still
visible). WAV files are read directly; anything else (or a WAV the stdlib cannot read, such as
float samples) is decoded with ffmpeg. Results are kept in memory per file version.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Any

from video_translator.utils.ffmpeg import FFmpegError, find_ffmpeg

VOICE_PEAKS_PER_SECOND = 50
BACKGROUND_PEAKS_PER_SECOND = 20
DECODE_RATE = 8000  # sample rate used when ffmpeg has to decode a file
CACHE_LIMIT = 4096
_CURVE = 0.7  # loudness ** 0.7: lifts quiet parts

_cache: dict[tuple[str, int, int, int], tuple[float, list[int]]] = {}


class WaveformError(RuntimeError):
    """The audio could not be read."""


def peaks_from_samples(samples: Any, rate: int, per_second: int) -> list[int]:
    """Peaks (0..100) of ``samples`` (numpy array scaled to -1..1), ``per_second`` per second."""
    import numpy as np

    if len(samples) == 0 or rate <= 0:
        return []
    size = max(1, int(round(rate / per_second)))
    count = -(-len(samples) // size)
    padded = np.zeros(count * size, dtype=np.float32)
    padded[: len(samples)] = np.abs(samples)
    loud = padded.reshape(count, size).max(axis=1)
    scaled = np.minimum(loud, 1.0) ** _CURVE * 100.0
    return [int(round(v)) for v in scaled]


def _read_wav(path: Path) -> tuple[Any, int] | None:
    """``(float samples -1..1 mono, rate)`` for an integer PCM wav, else None."""
    import numpy as np

    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        return None
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return None
    if channels > 1:
        usable = len(data) - len(data) % channels
        data = np.abs(data[:usable].reshape(-1, channels)).max(axis=1)
    return data, rate


def _decode_with_ffmpeg(path: Path) -> tuple[Any, int]:
    import numpy as np

    cmd = [
        find_ffmpeg(), "-v", "error", "-nostdin", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(DECODE_RATE), "-f", "s16le", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise WaveformError(f"Could not start ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-200:]
        raise WaveformError(f"ffmpeg could not read {path.name}: {detail}")
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0, DECODE_RATE


def file_peaks(path: Path, per_second: int) -> tuple[float, list[int]]:
    """``(duration seconds, peaks)`` of an audio file."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise WaveformError(f"{path.name} is not readable") from exc
    key = (str(path), stat.st_mtime_ns, stat.st_size, per_second)
    if key in _cache:
        return _cache[key]
    loaded = _read_wav(path)
    if loaded is None:
        try:
            loaded = _decode_with_ffmpeg(path)
        except FFmpegError as exc:  # ffmpeg missing
            raise WaveformError(str(exc)) from exc
    samples, rate = loaded
    result = (round(len(samples) / rate, 3), peaks_from_samples(samples, rate, per_second))
    if len(_cache) >= CACHE_LIMIT:
        _cache.pop(next(iter(_cache)))
    _cache[key] = result
    return result
