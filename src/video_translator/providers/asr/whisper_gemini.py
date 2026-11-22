"""Speech recognition = Whisper for WHEN, Gemini for WHAT.

1. faster-whisper (local, CPU) finds every spoken line and its exact start and end.
2. Gemini listens to the same audio and corrects the Chinese words of each line. It may not move,
   merge or split lines, so Gemini's weak timing can no longer shift the subtitles.
3. Speech that Gemini hears in a gap Whisper left empty is added, but only inside that gap.

If Gemini's answer cannot be read, Whisper's own text is kept (nothing is lost).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from video_translator.config import WhisperConfig
from video_translator.models import Segment
from video_translator.providers.asr.base import ASRProvider
from video_translator.providers.asr.cleanup import drop_fillers, is_filler
from video_translator.providers.asr.gemini import (
    ASRParseError,
    _parse_time,
)
from video_translator.providers.asr.whisper_timing import TimedLine, WhisperTimer, WhisperTransport
from video_translator.providers.gemini_client import GeminiClient
from video_translator.providers.registry import register

PROMPT = """You are a precise Mandarin Chinese transcription editor. The attached audio was \
already \
split into numbered lines with exact times by another program. Its draft text may contain wrong \
characters or wrong words.

For every numbered line, listen to the audio between its start and end (seconds from the beginning \
of the audio) and write exactly what is SAID there.

Rules:
- Keep the line numbers. Do NOT change, merge, split, add or reorder lines, and do NOT \
change times.
- Write Simplified Chinese, verbatim, with ordinary Chinese \
punctuation. Do NOT translate or summarise.
- A line may have no draft text: listen to that time range and write what is said.
- If a line is only music, noise, singing or silence (no spoken words), return an empty string "".
- Filler sounds and interjections ("嗯", "啊", "呃", "哦", "hmm", laughter, sighs) are NOT speech \
lines: return "" for a line that holds only these, and leave them out of the text of other lines.
- A line marked "censored": true covers a word that was bleeped (replaced by a beep tone). \
The word is usually still faintly audible under the beep. Write the word that was censored \
(for example the swear word), together with any other words spoken in that time range. If you \
cannot hear it, give the most likely word; return "" only if nothing is spoken there.
- Separately, if you hear clear speech that is NOT inside any line (and not music), list it in \
"missed" with approximate start/end seconds. If there is none, return an empty list.

Lines (JSON):
{lines}

Return ONLY strict JSON, no markdown, in exactly this shape:
{{"lines": [{{"id": 0, "text": "..."}}], "missed": [{{"start": 0.0, "end": 0.0, "text": "..."}}]}}
"""

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
MAX_MISSED_SECONDS = 12.0
MIN_MISSED_SECONDS = 0.4
_SIMILAR = 0.6


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= _SIMILAR


def parse_refinement(raw: str) -> tuple[dict[int, str], list[tuple[float, float, str]]]:
    """``({id: text}, [(start, end, text)])`` from Gemini's reply; raises ASRParseError."""
    text = raw.strip()
    try:
        data: Any = json.loads(_FENCE_RE.sub("", text).strip())
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        lines_raw = data.get("lines", data.get("segments"))
        missed_raw = data.get("missed") or []
    elif isinstance(data, list):
        lines_raw, missed_raw = data, []
    else:
        raise ASRParseError(f"Gemini did not return usable JSON: {raw[:200]!r}")
    if not isinstance(lines_raw, list):
        raise ASRParseError("Gemini JSON has no 'lines' list.")
    texts: dict[int, str] = {}
    for item in lines_raw:
        if isinstance(item, dict) and isinstance(item.get("id"), (int, float)):
            texts[int(item["id"])] = " ".join(str(item.get("text") or "").split())
    missed: list[tuple[float, float, str]] = []
    for item in missed_raw if isinstance(missed_raw, list) else []:
        if not isinstance(item, dict):
            continue
        start, end = _parse_time(item.get("start")), _parse_time(item.get("end"))
        words = " ".join(str(item.get("text") or "").split())
        if start is not None and end is not None and words:
            missed.append((start, end, words))
    return texts, missed


def add_missed(
    segments: list[Segment], missed: list[tuple[float, float, str]], duration: float | None
) -> tuple[list[Segment], int]:
    """Insert Gemini's 'missed' speech, clamped inside the gap that holds its start."""
    added = 0
    result = list(segments)
    for start, end, text in sorted(missed):
        if duration is not None and start >= duration:
            continue
        before = [s for s in result if s.start <= start]
        gap_start = max((s.end for s in before if s.end <= start + 0.05), default=0.0)
        if any(s.start <= start < s.end - 0.05 for s in result):
            continue  # inside an existing line, not a gap
        after = [s.start for s in result if s.start > start]
        gap_end = min(after) if after else (duration if duration is not None else end)
        new_start = max(start, gap_start)
        new_end = min(end, gap_end, new_start + MAX_MISSED_SECONDS)
        if new_end - new_start < MIN_MISSED_SECONDS:
            continue
        neighbours = [s for s in result if abs(s.start - new_start) < 15]
        if any(_similar(text, s.text_zh) for s in neighbours):
            continue  # an echo of a line we already have
        result.append(Segment(start=new_start, end=new_end, text_zh=text))
        result.sort(key=lambda s: (s.start, s.end))
        added += 1
    return result, added


@register("asr", "whisper_gemini")
class WhisperGeminiASR(ASRProvider):
    """Local Whisper timing + Gemini text. One Gemini request per audio chunk."""

    cache_tag = "whisper_gemini"

    def __init__(
        self,
        device: str = "cpu",
        client: GeminiClient | None = None,
        model: str = "gemini-3.6-flash",
        requests_per_minute: int = 10,
        whisper: WhisperConfig | None = None,
        whisper_transport: WhisperTransport | None = None,
        parse_retries: int = 1,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        self.client = client or GeminiClient(model=model, requests_per_minute=requests_per_minute)
        self.timer = WhisperTimer(whisper or WhisperConfig(), whisper_transport)
        self.parse_retries = parse_retries
        self.notes: list[str] = []  # plain-language remarks (fallbacks, added lines)

    def transcribe(
        self, audio_path: Path, language: str = "zh", duration: float | None = None
    ) -> list[Segment]:
        audio_path = Path(audio_path)
        lines = self.timer.lines(audio_path, language, duration)
        if not lines:
            return []  # no speech found: skip the Gemini request
        payload = []
        for i, x in enumerate(lines):
            item: dict[str, Any] = {"id": i, "start": round(x.start, 2), "end": round(x.end, 2)}
            if x.text:
                item["whisper_text"] = x.text
            if x.censored:
                item["censored"] = True
            payload.append(item)
        prompt = PROMPT.replace("{lines}", json.dumps(payload, ensure_ascii=False))
        texts, missed = self._ask(prompt, audio_path, lines)
        segments = [
            Segment(start=x.start, end=x.end, text_zh=texts.get(i, x.text))
            for i, x in enumerate(lines)
        ]
        segments = drop_fillers([s for s in segments if s.text_zh.strip()])
        missed = [m for m in missed if not is_filler(m[2])]
        segments, added = add_missed(segments, missed, duration)
        if added:
            self.notes.append(f"{added} line(s) Whisper missed were added from Gemini")
        return segments

    def _ask(
        self, prompt: str, audio_path: Path, lines: list[TimedLine]
    ) -> tuple[dict[int, str], list[tuple[float, float, str]]]:
        attempt = 0
        while True:
            raw = self.client.generate_text(prompt, audio_path=audio_path, json_mode=True)
            try:
                return parse_refinement(raw)
            except ASRParseError:
                if attempt >= self.parse_retries:
                    self.notes.append(
                        f"Gemini's reply for {audio_path.name} was unreadable; "
                        "Whisper's own text was kept"
                    )
                    return {}, []
                attempt += 1
