from __future__ import annotations

from pathlib import Path

import pytest

from video_translator.models import Job, JobStatus
from video_translator.pipeline import Pipeline
from video_translator.stages.base import PauseRequested, Stage


def make_stage(stage_name: str, calls: list[str], fail: bool = False, pause=None) -> Stage:
    class Dummy(Stage):
        name = stage_name

        def run(self, job, config):
            calls.append(stage_name)
            if fail:
                raise RuntimeError("boom")
            if pause is not None and not job.review_approved:
                raise pause
            return job

    return Dummy()


def new_job(tmp_path: Path) -> Job:
    return Job(input_path=Path("in.mp4"), work_dir=tmp_path / "work")


def test_runs_stages_in_order_and_reports_progress(tmp_path):
    calls: list[str] = []
    events: list[tuple[str, int, int]] = []
    pipeline = Pipeline([make_stage("a", calls), make_stage("b", calls)])

    job = pipeline.run(new_job(tmp_path), on_progress=lambda *args: events.append(args))

    assert calls == ["a", "b"]
    assert job.status == JobStatus.DONE
    assert job.completed_stages == ["a", "b"]
    assert events == [("a", 0, 2), ("b", 1, 2), ("done", 2, 2)]
    assert Job.load(job.work_dir).status == JobStatus.DONE


def test_failure_marks_job_failed_saves_error_and_stops(tmp_path):
    calls: list[str] = []
    job = new_job(tmp_path)
    pipeline = Pipeline([make_stage("a", calls, fail=True), make_stage("b", calls)])

    with pytest.raises(RuntimeError):
        pipeline.run(job)

    assert calls == ["a"]
    assert job.status == JobStatus.FAILED
    assert "boom" in job.error
    saved = Job.load(job.work_dir)
    assert saved.status == JobStatus.FAILED and "boom" in saved.error
    assert saved.completed_stages == [] and saved.current_stage == "a"


def test_pause_stops_cleanly_and_resume_continues(tmp_path):
    calls: list[str] = []
    pause = PauseRequested(JobStatus.AWAITING_REVIEW, "please review")
    pipeline = Pipeline(
        [make_stage("a", calls), make_stage("review", calls, pause=pause), make_stage("c", calls)]
    )
    job = pipeline.run(new_job(tmp_path))

    assert job.status == JobStatus.AWAITING_REVIEW
    assert job.message == "please review"
    assert job.completed_stages == ["a"]
    assert calls == ["a", "review"]

    job = Job.load(job.work_dir)
    job.review_approved = True
    job = pipeline.resume(job)

    assert calls == ["a", "review", "review", "c"]  # "a" was skipped on resume
    assert job.status == JobStatus.DONE
    assert job.completed_stages == ["a", "review", "c"]
    assert job.message == ""


def test_paused_status_for_quota(tmp_path):
    calls: list[str] = []
    pipeline = Pipeline(
        [make_stage("a", calls, pause=PauseRequested(JobStatus.PAUSED, "quota used up"))]
    )
    job = pipeline.run(new_job(tmp_path))
    assert job.status == JobStatus.PAUSED and job.message == "quota used up"


def test_resume_after_failure_skips_finished_stages(tmp_path):
    calls: list[str] = []
    job = new_job(tmp_path)
    with pytest.raises(RuntimeError):
        Pipeline([make_stage("a", calls), make_stage("b", calls, fail=True)]).run(job)
    assert job.completed_stages == ["a"]

    job = Pipeline([make_stage("a", calls), make_stage("b", calls)]).resume(Job.load(job.work_dir))
    assert calls == ["a", "b", "b"]
    assert job.status == JobStatus.DONE and job.error == ""
