"""Placeholder TTS provider; replace with a real local or cloud TTS backend."""

from __future__ import annotations

from pathlib import Path

from video_translator.providers.registry import register
from video_translator.providers.tts.base import TTSProvider


@register("tts", "placeholder")
class PlaceholderTTS(TTSProvider):
    """Stands in until a Vietnamese TTS backend is chosen."""

    def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> Path:
        raise NotImplementedError("TTS is not implemented yet")
