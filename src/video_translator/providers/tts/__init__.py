"""TTS providers (Vietnamese text to speech). Importing this package registers "elevenlabs"."""

from __future__ import annotations

from video_translator.providers.tts import elevenlabs  # noqa: F401
from video_translator.providers.tts.base import TTSProvider

__all__ = ["TTSProvider"]
