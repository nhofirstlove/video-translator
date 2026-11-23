"""Translation providers (Chinese text to Vietnamese text). Registers "gemini" and "ninerouter"."""

from __future__ import annotations

from video_translator.providers.translation import gemini, ninerouter  # noqa: F401
from video_translator.providers.translation.base import TranslationProvider

__all__ = ["TranslationProvider"]
