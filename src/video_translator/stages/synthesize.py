"""Turn each Vietnamese segment into a speech clip using a TTS provider.

Inputs:  job.segments (text_vi)
Outputs: one audio clip per segment (``<work_dir>/tts/seg_00012.wav``) in segment.audio_path

Resumable: clips that already exist are skipped, and the job is saved every 10 segments.
A provider error stops the stage with a message that names the failing line.
"""

from __future__ import annotations

from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.providers import registry
from video_translator.providers.tts import vieneu as _vieneu  # noqa: F401  (registers)
from video_translator.providers.tts.base import TTSError, TTSProvider
from video_translator.stages.base import Stage

SAVE_EVERY = 10


class SynthesizeStage(Stage):
    name = "synthesize"

    def __init__(self, provider: TTSProvider | None = None) -> None:
        self._provider = provider

    def _make_provider(self, config: AppConfig) -> TTSProvider:
        if self._provider is not None:
            return self._provider
        cls = registry.get("tts", config.providers.tts)
        return cls(config=config)

    def run(self, job: Job, config: AppConfig) -> Job:
        todo = [
            (i, seg)
            for i, seg in enumerate(job.segments)
            if seg.text_vi.strip() and not (seg.audio_path and Path(seg.audio_path).is_file())
        ]
        if not todo:
            return job

        provider = self._make_provider(config)
        ext = getattr(provider, "extension", ".wav")
        tts_dir = job.work_dir / "tts"
        tts_dir.mkdir(parents=True, exist_ok=True)
        total = len(todo)
        job.message = (
            "Preparing the voice (the first time it downloads the model, which can take a "
            "few minutes)"
        )
        voice = config.vieneu.voice
        for n, (index, seg) in enumerate(todo, start=1):
            out = tts_dir / f"seg_{index:05d}{ext}"
            try:
                provider.synthesize(seg.text_vi.strip(), out, voice)
            except TTSError as exc:
                job.save()
                raise TTSError(
                    f"The voice could not speak line {index + 1} "
                    f"(finished clips are kept; resume the job after fixing this): {exc}"
                ) from exc
            seg.audio_path = out
            job.message = f"Synthesizing voice: {n}/{total} lines"
            if n % SAVE_EVERY == 0:
                job.save()
        job.message = f"Synthesized {total} lines"
        return job
