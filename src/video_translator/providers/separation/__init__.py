"""Separation providers (vocals vs. background)."""

from __future__ import annotations

from video_translator.providers.separation import demucs  # noqa: F401
from video_translator.providers.separation.base import SeparationProvider

__all__ = ["SeparationProvider"]
