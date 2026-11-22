"""Translation providers (Chinese text to Vietnamese text). Registers "gemini"."""

from __future__ import annotations

from video_translator.providers.translation import gemini  # noqa: F401
from video_translator.providers.translation.base import TranslationProvider

__all__ = ["TranslationProvider"]
