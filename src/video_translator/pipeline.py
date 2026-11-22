"""Pipeline: runs an ordered list of stages over a Job, resumable."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus
from video_translator.stages.base import PauseRequested, Stage

# Called as on_progress(stage_name, completed_stages, total_stages).
ProgressCallback = Callable[[str, int, int], None]


class Pipeline:
    def __init__(self, stages: Sequence[Stage], config: AppConfig | None = None) -> None:
        self.stages = list(stages)
        self.config = config or AppConfig()

    def run(self, job: Job, on_progress: ProgressCallback | None = None) -> Job:
        """Run every stage in order, skipping those already in ``job.completed_stages``.

        The job is saved after every stage. A stage raising ``PauseRequested`` stops the run
        cleanly (status AWAITING_REVIEW or PAUSED). Any other exception marks the job FAILED,
        stores ``job.error``, saves, and is re-raised.
        """
        total = len(self.stages)
        job.status = JobStatus.RUNNING
        job.error = ""
        job.message = ""
        job.save()
        try:
            for index, stage in enumerate(self.stages):
                if stage.name in job.completed_stages:
                    continue
                job.current_stage = stage.name
                if on_progress:
                    on_progress(stage.name, index, total)
                job = stage.run(job, self.config)
                job.completed_stages.append(stage.name)
                job.save()
        except PauseRequested as pause:
            job.status = pause.status
            job.message = pause.message
            job.save()
            return job
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.save()
            raise
        job.status = JobStatus.DONE
        job.current_stage = ""
        job.save()
        if on_progress:
            on_progress("done", total, total)
        return job

    def resume(self, job: Job, on_progress: ProgressCallback | None = None) -> Job:
        """Continue a paused or failed job; finished stages are skipped."""
        return self.run(job, on_progress)
