"""Tests for the web job manager, using fake quick pipeline stages."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from video_translator.config import AppConfig, PathsConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.pipeline import Pipeline
from video_translator.stages.base import PauseRequested, Stage
from video_translator.web.jobs import (
    INTERRUPTED_MESSAGE,
    InvalidEdit,
    InvalidJobId,
    InvalidUpload,
    JobManager,
    JobNotFound,
    JobStateError,
)


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


class PrepareStage(Stage):
    name = "prepare"

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate

    def run(self, job, config):
        if self.gate is not None:
            assert self.gate.wait(10), "gate not released"
        job.segments = [
            Segment(start=0.0, end=1.5, text_zh="你好", text_vi="Xin chao"),
            Segment(start=1.5, end=3.0, text_zh="谢谢", text_vi="Cam on", flags=["too_long"]),
        ]
        return job


class ReviewPause(Stage):
    name = "review"

    def run(self, job, config):
        if not job.review_approved:
            raise PauseRequested(JobStatus.AWAITING_REVIEW, "Please check the translation.")
        return job


class RenderStage(Stage):
    name = "render"

    def run(self, job, config):
        video = job.work_dir / "final.mp4"
        srt = job.work_dir / "final.srt"
        video.write_bytes(b"video-bytes")
        srt.write_text("1\n00:00:00,000 --> 00:00:01,500\nXin chao\n", encoding="utf-8")
        job.artifacts["output"] = video
        job.artifacts["srt"] = srt
        return job


class FailStage(Stage):
    name = "boom"

    def run(self, job, config):
        raise RuntimeError("kaboom")


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(
            input=tmp_path / "input", work=tmp_path / "work", output=tmp_path / "output"
        )
    )


def make_factory(gate: threading.Event | None = None, stages=None):
    def factory(config):
        return Pipeline(
            stages or [PrepareStage(gate), ReviewPause(), RenderStage()],
            config,
        )

    return factory


@pytest.fixture
def manager(tmp_path):
    mgr = JobManager(make_config(tmp_path), make_factory())
    yield mgr
    mgr.shutdown()


def upload(mgr: JobManager, name: str = "clip.mp4", data: bytes = b"abc") -> Job:
    return mgr.create_job(name, io.BytesIO(data))


def test_create_job_streams_file_and_saves(manager, tmp_path):
    job = upload(manager, "clip.mp4", b"x" * (2 * 1024 * 1024 + 5))
    assert job.status == JobStatus.PENDING
    assert job.input_path == tmp_path / "input" / "clip.mp4"
    assert job.input_path.stat().st_size == 2 * 1024 * 1024 + 5
    assert (job.work_dir / "job.json").is_file()
    assert manager.get(job.id).id == job.id


def test_duplicate_names_are_not_overwritten(manager):
    a = upload(manager, "clip.mp4", b"1")
    b = upload(manager, "clip.mp4", b"2")
    assert a.input_path != b.input_path
    assert a.input_path.read_bytes() == b"1"
    assert b.input_path.name == "clip (2).mp4"


def test_upload_name_is_sanitized(manager, tmp_path):
    job = upload(manager, "../../evil\\..\\clip.MP4", b"1")
    assert job.input_path.parent == tmp_path / "input"
    assert job.input_path.name == "clip.MP4"


@pytest.mark.parametrize("name", ["notes.txt", "movie.exe", "noext", "", "a.mp4.sh"])
def test_bad_extension_rejected(manager, tmp_path, name):
    with pytest.raises(InvalidUpload):
        upload(manager, name)
    assert not (tmp_path / "input").exists() or not list((tmp_path / "input").iterdir())


def test_empty_upload_rejected_and_cleaned(manager, tmp_path):
    with pytest.raises(InvalidUpload):
        upload(manager, "clip.mp4", b"")
    assert list((tmp_path / "input").iterdir()) == []


@pytest.mark.parametrize("bad", ["../etc", "abc", "ABCDEF123456", "0123456789abc", "", "..", "a/b"])
def test_invalid_job_ids(manager, bad):
    with pytest.raises(InvalidJobId):
        manager.get(bad)


def test_unknown_job(manager):
    with pytest.raises(JobNotFound):
        manager.get("0123456789ab")


def test_run_to_review_edit_approve_done(manager):
    job = upload(manager)
    manager.start(job.id)
    wait_until(lambda: manager.get(job.id).status == JobStatus.AWAITING_REVIEW)
    assert manager.wait_idle()
    current = manager.get(job.id)
    assert current.message == "Please check the translation."
    assert [s.text_vi for s in current.segments] == ["Xin chao", "Cam on"]

    manager.save_edits(job.id, {0: "  Chao ban  "})
    assert manager.get(job.id).segments[0].text_vi == "Chao ban"
    assert Job.load(current.work_dir).segments[0].text_vi == "Chao ban"

    manager.approve(job.id)
    wait_until(lambda: manager.get(job.id).status == JobStatus.DONE)
    assert manager.wait_idle()
    done = manager.get(job.id)
    assert done.review_approved
    assert manager.progress(done)["fraction"] == 1.0


def test_edit_validation(manager):
    job = upload(manager)
    with pytest.raises(JobStateError):  # not awaiting review yet
        manager.save_edits(job.id, {0: "x"})
    manager.start(job.id)
    wait_until(lambda: manager.get(job.id).status == JobStatus.AWAITING_REVIEW)
    assert manager.wait_idle()
    for bad in ({5: "x"}, {-1: "x"}, {0: "   "}, {0: ""}, {}):
        with pytest.raises(InvalidEdit):
            manager.save_edits(job.id, bad)
    # a failed validation changes nothing, even if some edits were fine
    with pytest.raises(InvalidEdit):
        manager.save_edits(job.id, {0: "ok", 1: " "})
    assert manager.get(job.id).segments[0].text_vi == "Xin chao"


def test_approve_requires_review_state(manager):
    job = upload(manager)
    with pytest.raises(JobStateError):
        manager.approve(job.id)
    with pytest.raises(JobStateError):
        manager.resume(job.id, approve=True)


def test_failed_job_can_be_resumed(tmp_path):
    attempts = {"n": 0}

    class Flaky(Stage):
        name = "flaky"

        def run(self, job, config):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("kaboom")
            return job

    mgr = JobManager(make_config(tmp_path), make_factory(stages=[Flaky()]))
    job = upload(mgr)
    mgr.start(job.id)
    wait_until(lambda: mgr.get(job.id).status == JobStatus.FAILED)
    assert mgr.wait_idle()
    assert "kaboom" in mgr.get(job.id).error
    mgr.resume(job.id)
    wait_until(lambda: mgr.get(job.id).status == JobStatus.DONE)
    mgr.shutdown()


def test_pipeline_factory_error_marks_failed(tmp_path):
    def broken_factory(config):
        raise ValueError("no stages")

    mgr = JobManager(make_config(tmp_path), broken_factory)
    job = upload(mgr)
    mgr.start(job.id)
    wait_until(lambda: mgr.get(job.id).status == JobStatus.FAILED)
    assert "no stages" in mgr.get(job.id).error
    assert mgr.wait_idle()
    mgr.shutdown()


def test_second_job_is_queued_while_one_runs(tmp_path):
    gate = threading.Event()
    mgr = JobManager(make_config(tmp_path), make_factory(gate))
    first, second = upload(mgr, "a.mp4"), upload(mgr, "b.mp4")
    mgr.start(first.id)
    wait_until(lambda: mgr.is_running(first.id))
    mgr.start(second.id)
    assert mgr.is_queued(second.id)
    assert not mgr.is_running(second.id)
    assert "Waiting" in mgr.get(second.id).message
    with pytest.raises(JobStateError):
        mgr.resume(second.id)  # already waiting
    with pytest.raises(JobStateError):
        mgr.start(first.id)  # already running
    # a running job is never reported as paused and is listed with live state
    assert mgr.get(first.id).status == JobStatus.RUNNING
    gate.set()
    wait_until(lambda: mgr.get(first.id).status == JobStatus.AWAITING_REVIEW)
    wait_until(lambda: mgr.get(second.id).status == JobStatus.AWAITING_REVIEW)
    assert mgr.wait_idle()
    mgr.shutdown()


def test_running_job_on_disk_is_shown_as_interrupted(tmp_path):
    config = make_config(tmp_path)
    mgr = JobManager(config, make_factory())
    job = upload(mgr)
    stale = mgr.get(job.id)
    stale.status = JobStatus.RUNNING
    stale.current_stage = "transcribe"
    stale.save()

    restarted = JobManager(config, make_factory())
    shown = restarted.get(job.id)
    assert shown.status == JobStatus.PAUSED
    assert shown.message == INTERRUPTED_MESSAGE
    assert [j.status for j in restarted.list_jobs()] == [JobStatus.PAUSED]
    assert Job.load(stale.work_dir).status == JobStatus.PAUSED  # persisted too
    restarted.resume(job.id)  # resumable
    wait_until(lambda: restarted.get(job.id).status == JobStatus.AWAITING_REVIEW)
    restarted.shutdown()
    mgr.shutdown()


def test_list_jobs_newest_first_and_tolerates_broken(manager, tmp_path):
    first = upload(manager, "a.mp4")
    time.sleep(0.05)
    second = upload(manager, "b.mp4")
    work = tmp_path / "work"
    (work / "aaaaaaaaaaaa").mkdir()
    (work / "aaaaaaaaaaaa" / "job.json").write_text("{not json", encoding="utf-8")
    (work / "bbbbbbbbbbbb").mkdir()  # no job.json
    (work / "not-a-job").mkdir()
    (work / "stray.txt").write_text("x", encoding="utf-8")
    ids = [j.id for j in manager.list_jobs()]
    assert ids == [second.id, first.id]


def test_job_json_roundtrip_unicode(manager):
    job = upload(manager, "视频.mp4")
    text = (job.work_dir / "job.json").read_text(encoding="utf-8")
    assert json.loads(text)["input_path"].endswith("视频.mp4")
