"""Fit each synthesized clip into its segment's time slot (time-stretch, trim or pad).

Inputs:  job.segments (audio_path, start, end)
Outputs: segment.audio_path updated to the aligned clips
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class AlignStage(Stage):
    name = "align"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("align is not implemented yet")
