"""Placeholder for an LLM-based translation provider (local model or cloud API)."""

from __future__ import annotations

from video_translator.models import Segment
from video_translator.providers.registry import register
from video_translator.providers.translation.base import TranslationProvider


@register("translation", "llm")
class LLMTranslation(TranslationProvider):
    """Will prompt an LLM to translate Chinese segments into Vietnamese."""

    def translate(self, segments: list[Segment]) -> list[Segment]:
        raise NotImplementedError("LLM translation is not implemented yet")
