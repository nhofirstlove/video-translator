"""Mux the original video with the new audio (and burned-in subtitles if enabled) with ffmpeg.

Inputs:  job.input_path, artifacts['mixed_audio'], optional subtitle files
Outputs: artifacts['output'] (final mp4)
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class RenderStage(Stage):
    name = "render"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("render is not implemented yet")
