"""Transcribe Chinese speech into timed segments using an ASR provider.

Inputs:  artifacts['vocals']
Outputs: job.segments with start, end, text_zh
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class TranscribeStage(Stage):
    name = "transcribe"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("transcribe is not implemented yet")
