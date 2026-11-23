"""Whisper-only speech recognition: no cloud model, no key, no quota.

The speech detector finds each stretch of speech (a pause of ``whisper.pause_seconds`` starts a new
line) and faster-whisper writes the words, all on this computer. Compared with ``whisper_gemini``
nothing listens to the audio afterwards, so:

* words are not corrected from context (names and look-alike words can be wrong),
* a word bleeped out in the original has no text: it becomes the mark ``（哔）`` (or the faint
  words Whisper still caught) and the translator guesses it from the sentence around it,
* quiet speech the detector misses is not added later,
* filler sounds and Whisper's known invented phrases are removed with simple rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from video_translator.config import WhisperConfig
from video_translator.models import Segment
from video_translator.providers.asr.base import ASRProvider
from video_translator.providers.asr.cleanup import (
    BLEEP_MARK,
    drop_fillers,
    is_filler,
    is_hallucination,
)
from video_translator.providers.asr.whisper_timing import WhisperTimer, WhisperTransport
from video_translator.providers.registry import register


@register("asr", "whisper")
class WhisperOnlyASR(ASRProvider):
    """Local Whisper for both the timing and the words."""

    cache_tag = "whisper"

    def __init__(
        self,
        device: str = "cpu",
        whisper: WhisperConfig | None = None,
        whisper_transport: WhisperTransport | None = None,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        self.timer = WhisperTimer(whisper or WhisperConfig(), whisper_transport)
        self._marked = 0

    def transcribe(
        self, audio_path: Path, language: str = "zh", duration: float | None = None
    ) -> list[Segment]:
        lines = self.timer.lines(Path(audio_path), language, duration)
        segments: list[Segment] = []
        for line in lines:
            text = line.text.strip()
            if line.censored:
                heard = "".join(w.text for w in line.words).strip()
                text = heard if heard and not is_filler(heard) else BLEEP_MARK
                if text == BLEEP_MARK:
                    self._marked += 1
            if not text or is_hallucination(text):
                continue
            segments.append(Segment(start=line.start, end=line.end, text_zh=text))
        return drop_fillers(segments)

    @property
    def notes(self) -> list[str]:
        """Plain-language remarks shown in the job message."""
        if not self._marked:
            return []
        return [
            f"{self._marked} bleeped word(s) were marked {BLEEP_MARK} for the translator to guess; "
            "check them in the review step"
        ]
