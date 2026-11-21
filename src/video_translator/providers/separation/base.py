"""Interface for audio source separation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SeparationProvider(ABC):
    def __init__(self, device: str = "cpu", **options) -> None:
        self.device = device
        self.options = options

    @abstractmethod
    def separate(self, audio_path: Path, out_dir: Path) -> tuple[Path, Path]:
        """Split ``audio_path`` into ``(vocals_path, background_path)`` inside ``out_dir``."""
