"""Abstract base class for pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from video_translator.models import JobStatus

if TYPE_CHECKING:
    from video_translator.config import AppConfig
    from video_translator.models import Job


class PauseRequested(Exception):  # noqa: N818 - not an error, a clean stop signal
    """Raised by a stage to stop the pipeline cleanly (not a failure).

    ``status`` is ``JobStatus.AWAITING_REVIEW`` or ``JobStatus.PAUSED`` (e.g. quota used up).
    The stage is NOT marked complete, so it runs again on resume.
    """

    def __init__(self, status: JobStatus, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Stage(ABC):
    """One step of the pipeline.

    A stage reads what it needs from the ``Job`` (segments, artifacts), writes its
    files into ``job.work_dir``, records them in ``job.artifacts`` and returns the job.
    """

    name: ClassVar[str]

    @abstractmethod
    def run(self, job: Job, config: AppConfig) -> Job:
        """Execute the stage and return the updated job."""
