"""TTS providers (Vietnamese text to speech). Importing this package registers "vieneu"."""

from __future__ import annotations

from video_translator.providers.tts import vieneu  # noqa: F401
from video_translator.providers.tts.base import (
    TTSError,
    TTSProvider,
    TTSSetupError,
    TTSVoiceError,
)

__all__ = ["TTSError", "TTSProvider", "TTSSetupError", "TTSVoiceError"]
