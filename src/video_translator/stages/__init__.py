"""Pipeline stages, in execution order."""

from __future__ import annotations

from video_translator.stages.align import AlignStage
from video_translator.stages.base import Stage
from video_translator.stages.extract_audio import ExtractAudioStage
from video_translator.stages.mix import MixStage
from video_translator.stages.render import RenderStage
from video_translator.stages.separate_audio import SeparateAudioStage
from video_translator.stages.subtitles import SubtitlesStage
from video_translator.stages.synthesize import SynthesizeStage
from video_translator.stages.transcribe import TranscribeStage
from video_translator.stages.translate import TranslateStage

# The order here is the order the pipeline runs them.
ALL_STAGES: list[type[Stage]] = [
    ExtractAudioStage,
    SeparateAudioStage,
    TranscribeStage,
    TranslateStage,
    SynthesizeStage,
    AlignStage,
    MixStage,
    SubtitlesStage,
    RenderStage,
]

__all__ = ["ALL_STAGES", "Stage"]
