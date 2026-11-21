"""Turn each Vietnamese segment into a speech clip using a TTS provider.

Inputs:  job.segments (text_vi)
Outputs: one audio clip per segment, stored in segment.audio_path
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class SynthesizeStage(Stage):
    name = "synthesize"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("synthesize is not implemented yet")
