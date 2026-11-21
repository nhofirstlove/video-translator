"""Interface for text-to-speech providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TTSProvider(ABC):
    def __init__(self, device: str = "cpu", **options) -> None:
        self.device = device
        self.options = options

    @abstractmethod
    def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> Path:
        """Speak Vietnamese ``text`` into an audio file at ``out_path`` and return it."""
