"""Pause so the user can check and fix the Vietnamese text before dubbing.

Inputs:  job.segments (text_zh, text_vi), job.review_approved, config.review
Outputs: job.segments with corrected text_vi; raises PauseRequested(AWAITING_REVIEW)
         until the user approves.
"""

from __future__ import annotations

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus
from video_translator.review_file import (
    apply_review,
    file_edited_since_written,
    review_path,
    write_review_file,
)
from video_translator.stages.base import PauseRequested, Stage


class ReviewStage(Stage):
    name = "review"

    def run(self, job: Job, config: AppConfig) -> Job:
        if not config.review:
            return job
        path = review_path(job)
        if not job.review_approved:
            if not path.exists():  # never overwrite the user's edits
                write_review_file(job)
            message = (
                f"Open {path}, fix the Vietnamese lines, save, then run: "
                f"video-translator resume {job.work_dir} --approve"
            )
            job.message = message
            raise PauseRequested(JobStatus.AWAITING_REVIEW, message)
        # Approved: use the file only if the user changed it after we wrote it (otherwise the
        # web UI may have edited job.segments directly and the file would be stale).
        if path.exists() and file_edited_since_written(job):
            apply_review(job)
        return job
