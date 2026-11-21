"""ASR providers (speech to timed text)."""

from __future__ import annotations

from video_translator.providers.asr import faster_whisper  # noqa: F401
from video_translator.providers.asr.base import ASRProvider

__all__ = ["ASRProvider"]
