"""Translation providers (Chinese text to Vietnamese text)."""

from __future__ import annotations

from video_translator.providers.translation import llm  # noqa: F401
from video_translator.providers.translation.base import TranslationProvider

__all__ = ["TranslationProvider"]
