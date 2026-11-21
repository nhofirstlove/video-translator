"""Interface for speech recognition (ASR) providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from video_translator.models import Segment


class ASRProvider(ABC):
    def __init__(self, device: str = "cpu", **options) -> None:
        self.device = device
        self.options = options

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str = "zh") -> list[Segment]:
        """Return timed segments with ``text_zh`` filled in."""
