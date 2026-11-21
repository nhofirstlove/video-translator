"""Pipeline: runs an ordered list of stages over a Job."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus
from video_translator.stages.base import Stage

# Called as on_progress(stage_name, completed_stages, total_stages).
ProgressCallback = Callable[[str, int, int], None]


class Pipeline:
    def __init__(self, stages: Sequence[Stage], config: AppConfig | None = None) -> None:
        self.stages = list(stages)
        self.config = config or AppConfig()

    def run(self, job: Job, on_progress: ProgressCallback | None = None) -> Job:
        """Run every stage in order. Marks the job FAILED and re-raises on error."""
        total = len(self.stages)
        job.status = JobStatus.RUNNING
        try:
            for index, stage in enumerate(self.stages):
                if on_progress:
                    on_progress(stage.name, index, total)
                job = stage.run(job, self.config)
        except Exception:
            job.status = JobStatus.FAILED
            raise
        job.status = JobStatus.DONE
        if on_progress:
            on_progress("done", total, total)
        return job
