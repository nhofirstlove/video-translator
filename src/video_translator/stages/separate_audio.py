"""Split the audio into vocals and background (music + effects) using a separation provider.

Inputs:  artifacts['audio']
Outputs: artifacts['vocals'], artifacts['background']
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class SeparateAudioStage(Stage):
    name = "separate_audio"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("separate_audio is not implemented yet")
