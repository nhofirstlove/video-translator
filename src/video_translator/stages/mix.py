"""Place aligned clips on a timeline and mix the Vietnamese voice with the background track.

Inputs:  job.segments (aligned clips), artifacts['background']
Outputs: artifacts['mixed_audio']
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class MixStage(Stage):
    name = "mix"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("mix is not implemented yet")
