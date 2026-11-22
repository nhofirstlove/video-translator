"""Interface for text-to-speech providers, plus the errors they raise."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TTSError(RuntimeError):
    """A text to speech request failed. Base class of every provider error."""


class TTSSetupError(TTSError):
    """The voice engine is not installed, not reachable or could not be loaded."""


class TTSVoiceError(TTSError):
    """The chosen voice does not exist."""


class TTSProvider(ABC):
    #: file extension of the clips this provider writes
    extension = ".wav"

    def __init__(self, device: str = "cpu", **options) -> None:
        self.device = device
        self.options = options

    @abstractmethod
    def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> Path:
        """Speak Vietnamese ``text`` into an audio file at ``out_path`` and return it."""

    def list_voices(self) -> list[str]:
        """Names of the voices that can be chosen (empty when they cannot be listed)."""
        return []
