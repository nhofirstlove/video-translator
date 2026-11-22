"""SubRip (.srt) writing and parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_MIN_STEP = 0.05  # smallest gap/length enforced between cues, in seconds


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS,mmm``; rounds to whole milliseconds first (59.9996 -> 01:00)."""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _parse_timestamp(h: str, m: str, s: str, frac: str) -> float:
    millis = int(frac.ljust(3, "0"))
    return int(h) * 3600 + int(m) * 60 + int(s) + millis / 1000


def _split_long_word(word: str, width: int) -> list[str]:
    return [word[i : i + width] for i in range(0, len(word), width)]


def wrap_text(text: str, width: int) -> list[str]:
    """Greedy word wrap at spaces; words longer than ``width`` are hard-split."""
    words: list[str] = []
    for word in text.split():
        words.extend(_split_long_word(word, width) if len(word) > width else [word])
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _balanced_two_lines(text: str, width: int) -> list[str] | None:
    """Split ``text`` into two lines of similar length (both <= width), or None if impossible."""
    words = text.split()
    best: tuple[int, list[str]] | None = None
    for i in range(1, len(words)):
        first, second = " ".join(words[:i]), " ".join(words[i:])
        if len(first) <= width and len(second) <= width:
            diff = abs(len(first) - len(second))
            if best is None or diff < best[0]:
                best = (diff, [first, second])
    return best[1] if best else None


def _layout(text: str, max_chars: int, max_lines: int) -> list[list[str]]:
    """Break ``text`` into cues, each a list of at most ``max_lines`` lines."""
    if len(text) <= max_chars:
        return [[text]]
    if max_lines >= 2 and len(text) <= max_chars * 2:
        balanced = _balanced_two_lines(text, max_chars)
        if balanced:
            return [balanced]
    lines = wrap_text(text, max_chars)
    return [lines[i : i + max_lines] for i in range(0, len(lines), max_lines)]


@dataclass
class _Cue:
    start: float
    end: float
    lines: list[str]

    @property
    def chars(self) -> int:
        return sum(len(line) for line in self.lines)


def segments_to_srt(
    segments: Iterable[Any],
    text_attr: str = "text_vi",
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    min_duration: float = 1.0,
    max_cps: float = 21,
) -> str:
    """Render segments (objects with ``start``, ``end`` and ``text_attr``) as .srt text.

    Long text is wrapped at word boundaries; text that does not fit ``max_lines`` lines is split
    into several cues dividing the time in proportion to their length. Cues are extended up to
    ``min_duration`` and to the reading speed ``max_cps`` where the next cue allows, and never
    overlap. Empty text is skipped. Uses ``\\n`` newlines and ends with a newline.
    """
    max_chars_per_line = max(1, max_chars_per_line)
    max_lines = max(1, max_lines)
    cues: list[_Cue] = []
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    for seg in ordered:
        text = " ".join(str(getattr(seg, text_attr, "") or "").split())
        if not text:
            continue
        start = max(0.0, float(seg.start))
        end = max(float(seg.end), start + _MIN_STEP)
        groups = _layout(text, max_chars_per_line, max_lines)
        weights = [max(1, sum(len(line) for line in group)) for group in groups]
        total = sum(weights)
        cursor = start
        for group, weight in zip(groups, weights, strict=True):
            piece_end = cursor + (end - start) * weight / total
            cues.append(_Cue(cursor, piece_end, group))
            cursor = piece_end

    # Strictly increasing starts so that every cue can have a positive length.
    for prev, cue in zip(cues, cues[1:], strict=False):
        if cue.start < prev.start + _MIN_STEP:
            cue.start = prev.start + _MIN_STEP
            cue.end = max(cue.end, cue.start + _MIN_STEP)

    for i, cue in enumerate(cues):
        next_start = cues[i + 1].start if i + 1 < len(cues) else None
        if next_start is not None and next_start < cue.end:
            cue.end = next_start
        else:
            wanted = max(cue.end, cue.start + min_duration, cue.start + cue.chars / max_cps)
            cue.end = min(wanted, next_start) if next_start is not None else wanted

    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(
            f"{number}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            + "\n".join(cue.lines)
        )
    return "\n\n".join(blocks) + "\n" if blocks else ""


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    """Parse .srt text into ``(start, end, text)`` tuples (multi-line text joined by ``\\n``)."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip("\n").split("\n")
        for idx, line in enumerate(lines):
            match = _TIME_RE.search(line)
            if match:
                g = match.groups()
                start = _parse_timestamp(*g[0:4])
                end = _parse_timestamp(*g[4:8])
                cues.append((start, end, "\n".join(lines[idx + 1 :]).strip()))
                break
    return cues
