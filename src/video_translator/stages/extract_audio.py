"""Pull the audio track out of the input video with ffmpeg.

Inputs:  job.input_path (video)
Outputs: artifacts['audio'] (full original audio, e.g. WAV)
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage


class ExtractAudioStage(Stage):
    name = "extract_audio"

    def run(self, job: Job, config: AppConfig) -> Job:
        raise NotImplementedError("extract_audio is not implemented yet")
