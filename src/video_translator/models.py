"""Core data models shared by every stage."""

from __future__ import annotations

import uuid
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Segment(BaseModel):
    """One timed piece of speech. Stages fill in the fields progressively."""

    start: float  # seconds from the start of the video
    end: float
    text_zh: str = ""  # set by transcribe
    text_vi: str = ""  # set by translate
    audio_path: Path | None = None  # set by synthesize / align

    @property
    def duration(self) -> float:
        return self.end - self.start


class Job(BaseModel):
    """State of one video being translated; passed from stage to stage."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    input_path: Path
    work_dir: Path
    status: JobStatus = JobStatus.PENDING
    segments: list[Segment] = Field(default_factory=list)
    # Named files produced along the way, e.g. "audio", "vocals", "background", "output".
    artifacts: dict[str, Path] = Field(default_factory=dict)
