"""Placeholder for a faster-whisper ASR provider (local, GPU/CPU)."""

from __future__ import annotations

from pathlib import Path

from video_translator.models import Segment
from video_translator.providers.asr.base import ASRProvider
from video_translator.providers.registry import register


@register("asr", "faster_whisper")
class FasterWhisperASR(ASRProvider):
    """Will transcribe Chinese speech with faster-whisper."""

    def transcribe(self, audio_path: Path, language: str = "zh") -> list[Segment]:
        raise NotImplementedError("faster-whisper transcription is not implemented yet")
