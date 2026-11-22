"""Local speech timing with faster-whisper (CPU).

Whisper decides WHEN each line is spoken (start and end, usually within a fraction of a second).
The words it writes are only a first draft: Gemini corrects them in ``whisper_gemini.py``.
Everything here runs on your own computer; the model (about 0.8 GB for "medium") is downloaded
from Hugging Face on first use and cached for later runs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from video_translator.config import WhisperConfig

MIN_PIECE_SECONDS = 1.5  # never cut a long line into pieces shorter than this
_PUNCT = "。！？!?；;，,、…"
INITIAL_PROMPT = "以下是普通话的句子，使用简体中文。"


class WhisperError(RuntimeError):
    """The local speech-timing model is missing or failed."""


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class TimedLine:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    censored: bool = False  # covers a bleeped (beep-tone) word; Gemini writes what was said
    force: bool = False  # keep the line even though Whisper wrote no text for it


class WhisperTransport(Protocol):
    def transcribe(self, audio_path: Path, language: str) -> list[TimedLine]: ...


def split_long_line(line: TimedLine, max_seconds: float) -> list[TimedLine]:
    """Cut a line longer than ``max_seconds`` at its best pause or punctuation, recursively.

    Needs word times. Without them (or if no cut leaves two pieces of at least 1.5 s) the line is
    returned unchanged.
    """
    if line.end - line.start <= max_seconds or len(line.words) < 2:
        return [line]
    best_index, best_score = None, -1.0
    for i in range(1, len(line.words)):
        left, right = line.words[:i], line.words[i:]
        if left[-1].end - line.start < MIN_PIECE_SECONDS or line.end - right[0].start < (
            MIN_PIECE_SECONDS
        ):
            continue
        gap = max(0.0, right[0].start - left[-1].end)
        score = gap + (0.5 if left[-1].text.rstrip().endswith(tuple(_PUNCT)) else 0.0)
        # prefer cuts near the middle so pieces stay balanced
        mid = (line.start + line.end) / 2
        score -= abs(left[-1].end - mid) / max(line.end - line.start, 1.0) * 0.4
        if score > best_score:
            best_index, best_score = i, score
    if best_index is None:
        return [line]
    parts = []
    for words in (line.words[:best_index], line.words[best_index:]):
        parts.append(
            TimedLine(
                start=words[0].start,
                end=words[-1].end,
                text="".join(w.text for w in words).strip(),
                words=words,
            )
        )
    return [piece for part in parts for piece in split_long_line(part, max_seconds)]


def clean_lines(
    lines: list[TimedLine], max_seconds: float, duration: float | None
) -> list[TimedLine]:
    """Drop empty lines, split overlong ones, clamp to the file length, remove overlaps."""
    out: list[TimedLine] = []
    for line in sorted(lines, key=lambda x: (x.start, x.end)):
        if not line.text.strip() and not line.force:
            continue
        for piece in split_long_line(line, max_seconds):
            start, end = max(0.0, piece.start), piece.end
            if duration is not None:
                end = min(end, duration)
            if end - start < 0.1:
                continue
            out.append(
                TimedLine(start, end, piece.text.strip(), piece.words, piece.censored, piece.force)
            )
    for prev, cur in zip(out, out[1:], strict=False):
        if prev.end > cur.start:
            prev.end = max(prev.start + 0.1, cur.start)
    return out


def detect_speech_spans(samples: Any, options: dict[str, Any]) -> list[tuple[float, float]]:
    """Stretches of speech ``(start, end seconds)`` found by the speech detector in the audio.

    This is the Silero detector bundled with faster-whisper. It listens for "is someone
    speaking", not for words, so music-only gaps give no stretch, and a pause of
    ``min_silence_duration_ms`` or more ends one.
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    found = get_speech_timestamps(samples, VadOptions(**options))
    return [(item["start"] / 16000.0, item["end"] / 16000.0) for item in found]


def lines_from_spans(spans: list[tuple[float, float]], words: list[Word]) -> list[TimedLine]:
    """One line per speech stretch; each Whisper word goes to the stretch it was spoken in.

    A word belongs to the stretch holding its middle (or the nearest one within 0.3 s); words
    farther from any stretch are dropped. A stretch without words stays as an empty line that
    Gemini fills in (or blanks, if it is no speech). Line times are the stretch's own times.
    """
    grouped: list[list[Word]] = [[] for _ in spans]
    for word in words:
        mid = (word.start + word.end) / 2
        best, best_distance = None, 0.3
        for i, (a, b) in enumerate(spans):
            distance = 0.0 if a <= mid <= b else min(abs(mid - a), abs(mid - b))
            if distance <= best_distance:
                best, best_distance = i, distance
                if distance == 0.0:
                    break
        if best is not None:
            grouped[best].append(word)
    lines = []
    for (a, b), group in zip(spans, grouped, strict=True):
        text = "".join(w.text for w in group).strip()
        lines.append(TimedLine(a, b, text, group, force=True))
    return lines


def decode_audio_16k(audio_path: Path) -> Any:
    """The audio as a float32 numpy array (16 kHz mono) decoded with ffmpeg.

    faster-whisper normally decodes files itself with the PyAV package, but some PyAV versions
    break it (``open() got an unexpected keyword argument 'metadata_errors'``). Handing it the
    decoded samples avoids PyAV completely; ffmpeg is already required by this tool.
    """
    import subprocess

    import numpy as np

    from video_translator.utils.ffmpeg import find_ffmpeg

    cmd = [
        find_ffmpeg(), "-v", "error", "-nostdin", "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise WhisperError(f"ffmpeg could not read {Path(audio_path).name}: {detail}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# --- beep tones (bleeped swear words) --------------------------------------------------------

BEEP_MIN_HZ, BEEP_MAX_HZ = 800.0, 3000.0
BEEP_MIN_SECONDS = 0.35
BEEP_TONALITY = 0.85  # share of a 0.2 s window's energy that sits in one narrow frequency peak
BEEP_PAD = 0.05
MIN_SIDE_SECONDS = 0.3  # a piece of a line left next to a beep must be at least this long
PAUSE_SPLIT_SECONDS = 0.8  # a silence this long inside one Whisper line means two sentences


def detect_beeps(samples: Any, rate: int = 16000) -> list[tuple[float, float]]:
    """Spans (start, end seconds) of beep-like pure tones, such as a bleeped swear word.

    A beep is a steady narrow tone between 800 and 3000 Hz lasting at least 0.35 s; speech and
    music spread their energy over many frequencies, so they do not match.
    """
    import numpy as np

    window = int(0.2 * rate)
    hop = int(0.1 * rate)
    if len(samples) < window:
        return []
    shade = np.hanning(window)
    spans: list[list[float]] = []
    for pos in range(0, len(samples) - window + 1, hop):
        frame = samples[pos : pos + window]
        if float(np.abs(frame).max()) < 0.02:
            continue
        power = np.abs(np.fft.rfft(frame * shade)) ** 2
        peak = int(power.argmax())
        hz = peak * rate / window
        share = float(power[max(0, peak - 3) : peak + 4].sum() / max(power.sum(), 1e-12))
        if share < BEEP_TONALITY or not BEEP_MIN_HZ <= hz <= BEEP_MAX_HZ:
            continue
        start, end = pos / rate, (pos + window) / rate
        if spans and start - spans[-1][1] <= 0.11:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return [(a, b) for a, b in spans if b - a >= BEEP_MIN_SECONDS]


def _piece(line: TimedLine, start: float, end: float) -> TimedLine:
    """The part of ``line`` between two times; text comes from the words inside, if known."""
    inside = [w for w in line.words if start <= (w.start + w.end) / 2 < end]
    if inside:
        return TimedLine(
            max(start, inside[0].start),
            min(end, inside[-1].end),
            "".join(w.text for w in inside).strip(),
            inside,
        )
    return TimedLine(start, end, "", force=True)


def apply_beeps(lines: list[TimedLine], beeps: list[tuple[float, float]]) -> list[TimedLine]:
    """Cut lines at beep tones; every beep becomes its own ``censored`` line.

    The word under a beep is still spoken (faintly), so it needs its own line with its own timing.
    A line that overlaps a beep is split into the part before it and the part after it; a leftover
    shorter than 0.3 s is absorbed into the beep line.
    """
    result = list(lines)
    for beep_start, beep_end in beeps:
        bs, be = max(0.0, beep_start - BEEP_PAD), beep_end + BEEP_PAD
        cs, ce = bs, be
        kept: list[TimedLine] = []
        for line in result:
            if line.end <= bs or line.start >= be:
                kept.append(line)
                continue
            if bs - line.start >= MIN_SIDE_SECONDS:
                kept.append(_piece(line, line.start, bs))
            else:
                cs = min(cs, line.start)
            if line.end - be >= MIN_SIDE_SECONDS:
                kept.append(_piece(line, be, line.end))
            else:
                ce = max(ce, line.end)
        kept.append(TimedLine(cs, ce, "", censored=True, force=True))
        result = sorted(kept, key=lambda x: (x.start, x.end))
    return result


def split_at_pauses(line: TimedLine, gap: float = PAUSE_SPLIT_SECONDS) -> list[TimedLine]:
    """Split a line where its words show a silence of ``gap`` seconds or more."""
    if len(line.words) < 2:
        return [line]
    groups: list[list[Word]] = [[line.words[0]]]
    for prev, cur in zip(line.words, line.words[1:], strict=False):
        if cur.start - prev.end >= gap:
            groups.append([])
        groups[-1].append(cur)
    if len(groups) == 1:
        return [line]
    return [TimedLine(g[0].start, g[-1].end, "".join(w.text for w in g).strip(), g) for g in groups]


def _detect_beeps_in_file(audio_path: Path) -> list[tuple[float, float]]:
    try:
        return detect_beeps(decode_audio_16k(audio_path))
    except WhisperError:
        return []


class _FasterWhisperTransport:
    """The real thing: faster-whisper on the CPU. The model loads once per process."""

    _models: dict[tuple, Any] = {}

    def __init__(self, cfg: WhisperConfig) -> None:
        self.cfg = cfg

    def _model(self) -> Any:
        key = (self.cfg.model, self.cfg.compute_type, self.cfg.cpu_threads, self.cfg.model_dir)
        if key not in self._models:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise WhisperError(
                    "The faster-whisper package is not installed. Activate the virtual "
                    "environment and run `pip install faster-whisper` "
                    '(or `pip install -e ".[dev]"` again).'
                ) from exc
            try:
                self._models[key] = WhisperModel(
                    self.cfg.model,
                    device="cpu",
                    compute_type=self.cfg.compute_type,
                    cpu_threads=self.cfg.cpu_threads,
                    download_root=self.cfg.model_dir,
                )
            except Exception as exc:  # download or load problems
                raise WhisperError(
                    f"Could not load the Whisper model {self.cfg.model!r}: {exc}. The first run "
                    "downloads it from Hugging Face, so connect to the internet once and try "
                    "again."
                ) from exc
        return self._models[key]

    def _vad_options(self) -> dict[str, Any]:
        return {
            "threshold": self.cfg.vad_threshold,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": int(self.cfg.pause_seconds * 1000),
            "speech_pad_ms": 100,
        }

    def transcribe(self, audio_path: Path, language: str) -> list[TimedLine]:
        """One line per stretch of speech.

        The speech detector decides WHERE speech is (start, end, and a new line after every
        pause); Whisper only supplies the words, which are placed into the stretch they were
        spoken in. Words in silence or music-only gaps are dropped.
        """
        model = self._model()
        samples = decode_audio_16k(Path(audio_path))
        try:
            spans = detect_speech_spans(samples, self._vad_options())
            if not spans:
                return []
            segments, _info = model.transcribe(
                samples,
                language=(language or "zh")[:2],
                beam_size=self.cfg.beam_size,
                vad_filter=True,
                vad_parameters=self._vad_options(),
                word_timestamps=True,
                condition_on_previous_text=False,
                initial_prompt=INITIAL_PROMPT,
            )
            words: list[Word] = []
            for seg in segments:  # a generator: the work happens here
                if seg.words:
                    words += [Word(float(w.start), float(w.end), w.word) for w in seg.words]
                elif seg.text.strip():
                    words.append(Word(float(seg.start), float(seg.end), seg.text.strip()))
            return lines_from_spans(spans, words)
        except WhisperError:
            raise
        except Exception as exc:
            raise WhisperError(f"Whisper failed on {Path(audio_path).name}: {exc}") from exc


def _line_to_json(x: TimedLine) -> dict[str, Any]:
    return {
        "start": x.start,
        "end": x.end,
        "text": x.text,
        "words": [[w.start, w.end, w.text] for w in x.words],
    }


def _line_from_json(i: dict[str, Any]) -> TimedLine:
    words = [Word(float(a), float(b), str(t)) for a, b, t in i.get("words", [])]
    return TimedLine(float(i["start"]), float(i["end"]), str(i["text"]), words)


class WhisperTimer:
    """Timed lines for one audio chunk, cached next to the chunk so a resume never redoes them.

    The cache holds Whisper's raw lines (with word times) and the beep tones found in the audio;
    cutting at beeps, pauses and long lines is done each time it is read, so those settings can
    change without running Whisper again.
    """

    SCHEMA = 4

    def __init__(
        self,
        cfg: WhisperConfig,
        transport: WhisperTransport | None = None,
        beep_finder: Callable[[Path], list[tuple[float, float]]] | None = None,
    ) -> None:
        self.cfg = cfg
        self._transport = transport or _FasterWhisperTransport(cfg)
        self._beep_finder = beep_finder or _detect_beeps_in_file

    def _cache_path(self, audio_path: Path) -> Path:
        return audio_path.with_suffix(".whisper.json")

    def _settings(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "model": self.cfg.model,
            "compute_type": self.cfg.compute_type,
            "pause_seconds": self.cfg.pause_seconds,
            "vad_threshold": self.cfg.vad_threshold,
        }

    def _raw(
        self, audio_path: Path, language: str
    ) -> tuple[list[TimedLine], list[tuple[float, float]]]:
        path = self._cache_path(audio_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("settings") == self._settings():
                return (
                    [_line_from_json(i) for i in data["lines"]],
                    [(float(a), float(b)) for a, b in data["beeps"]],
                )
        except (OSError, ValueError, KeyError, TypeError):
            pass
        lines = self._transport.transcribe(audio_path, language)
        beeps = self._beep_finder(audio_path)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "settings": self._settings(),
                    "beeps": [[a, b] for a, b in beeps],
                    "lines": [_line_to_json(x) for x in lines],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return lines, beeps

    def lines(
        self, audio_path: Path, language: str = "zh", duration: float | None = None
    ) -> list[TimedLine]:
        raw, beeps = self._raw(audio_path, language)
        lines = [TimedLine(x.start, x.end, x.text, list(x.words)) for x in raw]
        if self.cfg.detect_beeps:
            lines = apply_beeps(lines, beeps)
        lines = [piece for line in lines for piece in split_at_pauses(line)]
        return clean_lines(lines, self.cfg.max_segment_seconds, duration)
