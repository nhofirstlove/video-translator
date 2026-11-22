"""ASR providers (speech to timed text). Importing this package registers "gemini"."""

from __future__ import annotations

from video_translator.providers.asr import gemini  # noqa: F401
from video_translator.providers.asr.base import ASRProvider

__all__ = ["ASRProvider"]
