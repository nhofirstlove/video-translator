"""Build a Vietnamese .srt file and, if configured, a styled file for burn-in.

Inputs:  job.segments (text_vi), config.subtitles
Outputs: artifacts['srt'] and optionally artifacts['burn_in_subs']
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class SubtitlesStage(Stage):
    name = "subtitles"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("subtitles is not implemented yet")
