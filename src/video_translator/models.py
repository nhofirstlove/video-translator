"""Core data models shared by every stage.

Artifact keys (``Job.artifacts``) used by the version 1 pipeline:

- ``"audio"``: full-length 44.1 kHz stereo wav extracted from the video.
- ``"vocals"``, ``"background"``: the separated voice and the rest (only with ``separate_voice``).
- ``"voice_track"``: aligned Vietnamese voice-only wav, full length.
- ``"mixed_audio"``: the final audio (voice track mixed with the original sound).
- ``"srt"``: Vietnamese subtitles file.
- ``"output"``: the final mp4.
"""

from __future__ import annotations

import json
import os
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from video_translator.paths import job_dir

JOB_FILE_NAME = "job.json"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"  # paused until the user approves the translation
    PAUSED = "paused"  # e.g. API quota exhausted; resumable
    DONE = "done"
    FAILED = "failed"


class Chunk(BaseModel):
    """One audio file covering a time range of the full video."""

    index: int
    start: float  # seconds in the full video
    end: float
    path: Path


class Segment(BaseModel):
    """One timed piece of speech. Stages fill in the fields progressively."""

    start: float  # seconds from the start of the video
    end: float
    text_zh: str = ""  # set by transcribe
    text_vi: str = ""  # set by translate
    audio_path: Path | None = None  # set by synthesize
    aligned_path: Path | None = None  # set by align
    flags: list[str] = Field(default_factory=list)  # e.g. "too_long"
    speed: float | None = None  # user-chosen tempo of the raw clip (editor); None = automatic
    audio_stale: bool = False  # the text changed after the clip was made (editor)

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
    # Named files produced along the way; see the module docstring for the keys.
    artifacts: dict[str, Path] = Field(default_factory=dict)
    chunks: list[Chunk] = Field(default_factory=list)
    completed_stages: list[str] = Field(default_factory=list)
    review_approved: bool = False
    error: str = ""
    message: str = ""  # human-readable progress or pause text
    current_stage: str = ""
    # Volumes chosen in the editor (keys of ``AudioConfig``); they win over the config values.
    audio_overrides: dict[str, float] = Field(default_factory=dict)
    # Blur-box settings chosen for this video in the editor (keys of ``BlurConfig``).
    blur_overrides: dict[str, Any] = Field(default_factory=dict)
    # Switched on per video: Demucs splits the voice from the music first, and the voice alone is
    # used for the timing and the transcription (the final mix still uses the original sound).
    separate_voice: bool = False

    @classmethod
    def create(cls, input_path: str | Path, work_root: str | Path) -> Job:
        """Make a new job whose folder is ``work_root/<id>`` (created on disk)."""
        job_id = uuid.uuid4().hex[:12]
        work_dir = job_dir(work_root, job_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        return cls(id=job_id, input_path=Path(input_path), work_dir=work_dir)

    def save(self) -> Path:
        """Write ``work_dir/job.json`` atomically (temp file + replace)."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        target = self.work_dir / JOB_FILE_NAME
        tmp = self.work_dir / (JOB_FILE_NAME + ".tmp")
        data = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2)
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, work_dir: str | Path) -> Job:
        """Read the job saved in ``work_dir``; ``work_dir`` is taken from where it was found."""
        work_dir = Path(work_dir)
        data = json.loads((work_dir / JOB_FILE_NAME).read_text(encoding="utf-8"))
        job = cls.model_validate(data)
        job.work_dir = work_dir
        return job
