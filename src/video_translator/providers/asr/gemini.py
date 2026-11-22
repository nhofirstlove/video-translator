"""Speech recognition with Gemini: one audio chunk in, timed Chinese segments out."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from video_translator.models import Segment
from video_translator.providers.asr.base import ASRProvider
from video_translator.providers.gemini_client import GeminiClient, GeminiError
from video_translator.providers.registry import register

PROMPT = """You are a precise speech transcription engine. Transcribe the attached audio.

Rules:
- The speech is Mandarin Chinese. Transcribe it VERBATIM, word for word, in Simplified Chinese \
(convert any Traditional characters to Simplified). Do NOT translate and do NOT summarise.
- Ignore background music, sound effects, laughter and noise. Do not describe them.
- Do not invent text. If a passage is unintelligible, skip it instead of guessing.
- Split the speech into natural sentences or phrases, roughly 1 to 12 seconds each. Never merge \
several sentences into one long segment.
- Use ordinary Chinese punctuation inside the text.
- "start" and "end" are in seconds (decimal numbers) measured from the beginning of THIS audio \
file, and must be accurate to about 0.2 seconds. Segments must be in time order and must not \
overlap.
- If there is no speech at all, return an empty list.

Return ONLY strict JSON, with no markdown and no commentary, in exactly this shape:
{"segments": [{"start": 0.0, "end": 3.2, "text": "..."}]}
"""

_MAX_GUESSED = 8.0  # a missing end is guessed at most this long after the start
_MIN_LEN = 0.2  # shortest segment length produced by clamping, in seconds
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
_OBJECT_RE = re.compile(r"\{[^{}]*\}")


class ASRParseError(GeminiError):
    """Gemini's reply could not be understood as a list of segments."""


def _parse_time(value: Any) -> float | None:
    """Accept seconds as number/string, or ``mm:ss(.fff)`` / ``hh:mm:ss(.fff)`` strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower().rstrip("s").strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) > 3:
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def _load_items(raw: str) -> list[Any]:
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Salvage: a truncated reply still holds complete {...} objects.
        items = []
        for match in _OBJECT_RE.finditer(text):
            try:
                items.append(json.loads(match.group(0)))
            except json.JSONDecodeError:
                continue
        items = [i for i in items if isinstance(i, dict) and "text" in i]
        if not items:
            raise ASRParseError(f"Gemini did not return valid JSON: {raw[:200]!r}") from None
        return items
    if isinstance(data, dict):
        for key in ("segments", "transcript", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        if "text" in data:
            return [data]
        raise ASRParseError("Gemini JSON has no 'segments' list.")
    if isinstance(data, list):
        return data
    raise ASRParseError("Gemini JSON is neither an object nor a list.")


def parse_segments(raw: str, duration: float | None = None) -> list[Segment]:
    """Turn Gemini's JSON reply into clean, sorted, non-overlapping segments.

    Tolerates code fences, ``mm:ss`` times, missing ends and truncated replies. Empty text is
    dropped, ``end`` is forced above ``start``, times are clamped to ``duration`` if known.
    """
    rows: list[tuple[float, float | None, str]] = []
    for item in _load_items(raw):
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text", item.get("text_zh", "")) or "").split())
        start = _parse_time(item.get("start"))
        if not text or start is None:
            continue
        rows.append((max(0.0, start), _parse_time(item.get("end")), text))
    rows.sort(key=lambda r: r[0])

    segments: list[Segment] = []
    for i, (start, end, text) in enumerate(rows):
        if duration is not None and start >= duration:
            continue
        if end is None or end <= start:
            nxt = rows[i + 1][0] if i + 1 < len(rows) else None
            end = min(nxt, start + _MAX_GUESSED) if nxt is not None and nxt > start else start + 2.0
        if duration is not None:
            end = min(end, duration)
            if end <= start:
                continue
        segments.append(Segment(start=start, end=end, text_zh=text))

    for prev, cur in zip(segments, segments[1:], strict=False):
        if prev.end > cur.start:
            if cur.start - prev.start >= _MIN_LEN:
                prev.end = cur.start
            else:  # (nearly) the same start: keep both by pushing the later one back
                cur.start = prev.end
                if cur.end <= cur.start:
                    cur.end = cur.start + _MIN_LEN
    return segments


@register("asr", "gemini")
class GeminiASR(ASRProvider):
    """Transcribes Mandarin audio with Gemini (free tier friendly: one request per chunk)."""

    def __init__(
        self,
        device: str = "cpu",
        client: GeminiClient | None = None,
        model: str = "gemini-3.6-flash",
        requests_per_minute: int = 10,
        parse_retries: int = 1,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        self.client = client or GeminiClient(model=model, requests_per_minute=requests_per_minute)
        self.parse_retries = parse_retries

    def transcribe(
        self, audio_path: Path, language: str = "zh", duration: float | None = None
    ) -> list[Segment]:
        """Transcribe one chunk; times are seconds from the start of ``audio_path``."""
        prompt = PROMPT
        if language and not language.lower().startswith("zh"):
            prompt = prompt.replace("Mandarin Chinese", f"language code {language!r}")
        attempt = 0
        while True:
            raw = self.client.generate_text(prompt, audio_path=Path(audio_path), json_mode=True)
            try:
                return parse_segments(raw, duration)
            except ASRParseError:
                if attempt >= self.parse_retries:
                    raise
                attempt += 1
