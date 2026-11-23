"""Pipeline stages, in execution order."""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.stages.align import AlignStage
from video_translator.stages.base import Stage
from video_translator.stages.extract_audio import ExtractAudioStage
from video_translator.stages.mix import MixStage
from video_translator.stages.render import RenderStage
from video_translator.stages.review import ReviewStage
from video_translator.stages.separate_audio import SeparateAudioStage
from video_translator.stages.subtitles import SubtitlesStage
from video_translator.stages.synthesize import SynthesizeStage
from video_translator.stages.transcribe import TranscribeStage
from video_translator.stages.translate import TranslateStage

# Every stage including version 2 ones (separate_audio), in execution order.
ALL_STAGES: list[type[Stage]] = [
    ExtractAudioStage,
    SeparateAudioStage,
    TranscribeStage,
    TranslateStage,
    ReviewStage,
    SubtitlesStage,
    SynthesizeStage,
    AlignStage,
    MixStage,
    RenderStage,
]

# The stages that run: separate_audio passes through unless the job asks for it.
V1_STAGES: list[type[Stage]] = [
    ExtractAudioStage,
    SeparateAudioStage,  # does nothing unless the job has separate_voice switched on
    TranscribeStage,
    TranslateStage,
    ReviewStage,
    SubtitlesStage,
    SynthesizeStage,
    AlignStage,
    MixStage,
    RenderStage,
]


def build_stages(config: AppConfig) -> list[Stage]:
    """Instances of the version 1 stages, in execution order."""
    return [stage_cls() for stage_cls in V1_STAGES]


__all__ = ["ALL_STAGES", "V1_STAGES", "Stage", "build_stages"]
