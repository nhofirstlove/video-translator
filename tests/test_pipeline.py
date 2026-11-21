from __future__ import annotations

from pathlib import Path

import pytest

from video_translator.models import Job, JobStatus
from video_translator.pipeline import Pipeline
from video_translator.stages.base import Stage


def make_stage(stage_name: str, calls: list[str], fail: bool = False) -> Stage:
    class Dummy(Stage):
        name = stage_name

        def run(self, job, config):
            calls.append(stage_name)
            if fail:
                raise RuntimeError("boom")
            return job

    return Dummy()


def new_job() -> Job:
    return Job(input_path=Path("in.mp4"), work_dir=Path("work"))


def test_runs_stages_in_order_and_reports_progress():
    calls: list[str] = []
    events: list[tuple[str, int, int]] = []
    pipeline = Pipeline([make_stage("a", calls), make_stage("b", calls)])

    job = pipeline.run(new_job(), on_progress=lambda *args: events.append(args))

    assert calls == ["a", "b"]
    assert job.status == JobStatus.DONE
    assert events == [("a", 0, 2), ("b", 1, 2), ("done", 2, 2)]


def test_failure_marks_job_failed_and_stops():
    calls: list[str] = []
    job = new_job()
    pipeline = Pipeline([make_stage("a", calls, fail=True), make_stage("b", calls)])

    with pytest.raises(RuntimeError):
        pipeline.run(job)

    assert calls == ["a"]
    assert job.status == JobStatus.FAILED
