"""Abstract base class for pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from video_translator.config import AppConfig
    from video_translator.models import Job


class Stage(ABC):
    """One step of the pipeline.

    A stage reads what it needs from the ``Job`` (segments, artifacts), writes its
    files into ``job.work_dir``, records them in ``job.artifacts`` and returns the job.
    """

    name: ClassVar[str]

    @abstractmethod
    def run(self, job: Job, config: AppConfig) -> Job:
        """Execute the stage and return the updated job."""
