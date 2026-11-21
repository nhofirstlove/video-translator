"""Swappable providers (local or cloud) for each AI step.

Importing this package registers the built-in implementations.
"""

from __future__ import annotations

from video_translator.providers import asr, separation, translation, tts  # noqa: F401
from video_translator.providers.registry import available, get, register

__all__ = ["available", "get", "register"]
