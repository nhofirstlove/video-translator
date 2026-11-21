"""Interface for translation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from video_translator.models import Segment


class TranslationProvider(ABC):
    def __init__(self, device: str = "cpu", **options) -> None:
        self.device = device
        self.options = options

    @abstractmethod
    def translate(self, segments: list[Segment]) -> list[Segment]:
        """Return the segments with ``text_vi`` filled in from ``text_zh``."""
