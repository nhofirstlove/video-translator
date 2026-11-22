"""Turn each Vietnamese segment into a speech clip using a TTS provider.

Inputs:  job.segments (text_vi)
Outputs: one audio clip per segment (``<work_dir>/tts/seg_00012.mp3``) in segment.audio_path

Paid-quota safe: the total characters still to speak are checked against the monthly budget
BEFORE any request; clips already made are never paid for again (resume skips them), and the
job is saved every 10 segments.
"""

from __future__ import annotations

from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus
from video_translator.providers import registry
from video_translator.providers.tts import elevenlabs as _elevenlabs  # noqa: F401  (registers)
from video_translator.providers.tts.base import TTSProvider
from video_translator.providers.tts.elevenlabs import TTSQuotaExceededError
from video_translator.stages.base import PauseRequested, Stage
from video_translator.usage import UsageTracker, default_usage_path

SAVE_EVERY = 10


def _chars(text: str) -> int:
    return len(text.strip())


class SynthesizeStage(Stage):
    name = "synthesize"

    def __init__(
        self, provider: TTSProvider | None = None, tracker: UsageTracker | None = None
    ) -> None:
        self._provider = provider
        self._tracker = tracker

    def _make_provider(self, config: AppConfig) -> TTSProvider:
        if self._provider is not None:
            return self._provider
        cls = registry.get("tts", config.providers.tts)
        return cls(config=config)

    def _make_tracker(self, config: AppConfig) -> UsageTracker:
        if self._tracker is not None:
            return self._tracker
        return UsageTracker(
            default_usage_path(config.paths.work), config.elevenlabs.monthly_character_budget
        )

    def run(self, job: Job, config: AppConfig) -> Job:
        tracker = self._make_tracker(config)
        todo = [
            (i, seg)
            for i, seg in enumerate(job.segments)
            if seg.text_vi.strip() and not (seg.audio_path and Path(seg.audio_path).is_file())
        ]
        needed = sum(_chars(seg.text_vi) for _, seg in todo)
        if needed > tracker.remaining:
            raise PauseRequested(
                JobStatus.PAUSED,
                f"Not enough ElevenLabs characters for this video: it needs {needed} but only "
                f"{tracker.remaining} are left this month (budget {tracker.monthly_budget}, "
                f"used {tracker.used}). Nothing was synthesized. Options: upgrade your "
                "ElevenLabs plan and raise `elevenlabs.monthly_character_budget` in "
                "config/local.yaml, wait for next month, or shorten the video / the lines. "
                "Then resume the job.",
            )
        if not todo:
            return job

        provider = self._make_provider(config)
        ext = getattr(provider, "extension", ".mp3")
        tts_dir = job.work_dir / "tts"
        tts_dir.mkdir(parents=True, exist_ok=True)
        total = len(todo)
        for n, (index, seg) in enumerate(todo, start=1):
            out = tts_dir / f"seg_{index:05d}{ext}"
            try:
                provider.synthesize(seg.text_vi.strip(), out)
            except TTSQuotaExceededError as exc:
                job.save()
                raise PauseRequested(
                    JobStatus.PAUSED,
                    f"ElevenLabs quota used up after {n - 1} of {total} lines "
                    f"(finished clips are kept). {exc} Upgrade the plan or wait for the "
                    "monthly reset, then resume the job.",
                ) from exc
            seg.audio_path = out
            tracker.record(_chars(seg.text_vi))
            job.message = f"Synthesizing voice: {n}/{total} lines"
            if n % SAVE_EVERY == 0:
                job.save()
        job.message = f"Synthesized {total} lines"
        return job
