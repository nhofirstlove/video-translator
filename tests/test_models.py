from __future__ import annotations

import json
from pathlib import Path

from video_translator.models import Chunk, Job, JobStatus, Segment


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


def test_new_job_fields_and_statuses():
    job = Job(input_path=Path("in.mp4"), work_dir=Path("w"))
    assert job.chunks == [] and job.completed_stages == []
    assert job.review_approved is False
    assert job.error == "" and job.message == "" and job.current_stage == ""
    assert JobStatus.AWAITING_REVIEW.value == "awaiting_review"
    assert JobStatus.PAUSED.value == "paused"
    seg = Segment(start=0, end=1)
    assert seg.aligned_path is None and seg.flags == []


def test_job_save_load_round_trip(tmp_path):
    work = tmp_path / "work" / "j1"
    job = Job(id="j1", input_path=Path("in.mp4"), work_dir=work)
    job.segments = [Segment(start=0, end=2, text_zh="你好", text_vi="Xin chào", flags=["too_long"])]
    job.chunks = [Chunk(index=0, start=0, end=600, path=work / "chunk_0.wav")]
    job.artifacts["audio"] = work / "audio.wav"
    job.completed_stages = ["extract_audio"]
    job.status = JobStatus.PAUSED
    job.message = "quota"
    saved = job.save()
    assert saved == work / "job.json"
    assert not (work / "job.json.tmp").exists()
    loaded = Job.load(work)
    assert loaded == job


def test_job_round_trip_windows_and_unicode_paths(tmp_path):
    work = tmp_path / "công việc 视频"
    job = Job(
        input_path=Path("视频 bài giảng.mp4"), work_dir=work, artifacts={"audio": work / "âm.wav"}
    )
    job.segments = [Segment(start=0, end=1, audio_path=work / "ảnh.wav")]
    job.save()
    text = (work / "job.json").read_text(encoding="utf-8")
    assert "视频" in text  # stored as readable Unicode, not escapes
    loaded = Job.load(work)
    assert loaded.input_path == Path("视频 bài giảng.mp4")
    assert loaded.artifacts["audio"] == work / "âm.wav"
    # Windows-style path strings in the JSON load fine as path strings.
    data = json.loads(text)
    data["input_path"] = "C:\\Users\\Hà\\Videos\\clip.mp4"
    (work / "job.json").write_text(json.dumps(data), encoding="utf-8")
    assert str(Job.load(work).input_path).endswith("clip.mp4")


def test_job_create_makes_folder(tmp_path):
    job = Job.create(tmp_path / "in.mp4", tmp_path / "work")
    assert job.work_dir == tmp_path / "work" / job.id
    assert job.work_dir.is_dir()
    assert job.input_path == tmp_path / "in.mp4"
