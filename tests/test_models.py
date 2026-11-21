from __future__ import annotations

from pathlib import Path

from video_translator.models import Job, JobStatus, Segment


def test_segment_defaults_and_duration():
    seg = Segment(start=1.0, end=3.5)
    assert seg.duration == 2.5
    assert seg.text_zh == "" and seg.text_vi == ""
    assert seg.audio_path is None


def test_job_defaults():
    job = Job(input_path=Path("in.mp4"), work_dir=Path("work/1"))
    assert job.status == JobStatus.PENDING
    assert job.segments == [] and job.artifacts == {}
    assert job.id


def test_job_ids_are_unique():
    a = Job(input_path=Path("a.mp4"), work_dir=Path("w"))
    b = Job(input_path=Path("a.mp4"), work_dir=Path("w"))
    assert a.id != b.id
