"""Placeholder for a Demucs-based separation provider (local, GPU/CPU)."""

from __future__ import annotations

from pathlib import Path

from video_translator.providers.registry import register
from video_translator.providers.separation.base import SeparationProvider


@register("separation", "demucs")
class DemucsSeparation(SeparationProvider):
    """Will run Demucs in two-stem mode: vocals vs. everything else."""

    def separate(self, audio_path: Path, out_dir: Path) -> tuple[Path, Path]:
        raise NotImplementedError("Demucs separation is not implemented yet")
