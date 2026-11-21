"""Translate each Chinese segment into Vietnamese using a translation provider.

Inputs:  job.segments (text_zh)
Outputs: job.segments with text_vi filled in
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class TranslateStage(Stage):
    name = "translate"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("translate is not implemented yet")
