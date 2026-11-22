"""Tests for the web API with FastAPI's TestClient and fake quick pipeline stages."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_translator import doctor
from video_translator.config import AppConfig, PathsConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.pipeline import Pipeline
from video_translator.stages.base import PauseRequested, Stage
from video_translator.web.app import create_app


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


def make_client(tmp_path: Path, gate: threading.Event | None = None) -> TestClient:
    config = AppConfig(
        paths=PathsConfig(
            input=tmp_path / "input", work=tmp_path / "work", output=tmp_path / "output"
        )
    )

    def factory(cfg):
        return Pipeline([PrepareStage(gate), ReviewPause(), RenderStage()], cfg)

    return TestClient(create_app(config, factory))


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as test_client:
        yield test_client
        test_client.app.state.jobs.shutdown()


def post_video(client, name="clip.mp4", data=b"fake video", **params):
    return client.post("/api/jobs", files={"file": (name, data, "video/mp4")}, params=params)


def status_of(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()["status"]


def wait_status(client, job_id, status):
    wait_until(lambda: status_of(client, job_id) == status)


def job_in_review(client, name="clip.mp4"):
    job_id = post_video(client, name).json()["id"]
    wait_status(client, job_id, "awaiting_review")
    return job_id


def test_health_and_index(client):
    assert client.get("/health").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "Video Translator" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_upload_creates_and_starts_job(client, tmp_path):
    res = post_video(client)
    assert res.status_code == 200
    body = res.json()
    assert len(body["id"]) == 12
    assert body["filename"] == "clip.mp4"
    assert (tmp_path / "input" / "clip.mp4").read_bytes() == b"fake video"
    wait_status(client, body["id"], "awaiting_review")
    info = client.get(f"/api/jobs/{body['id']}").json()
    assert info["segments"] == 2
    assert info["message"] == "Please check the translation."
    assert info["current_stage"] == "review"
    assert info["progress"]["total"] == 3
    assert info["downloads"] == {"video": False, "srt": False}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [body["id"]]


def test_upload_without_start_stays_pending_then_resume_starts(client):
    job_id = post_video(client, start="false").json()["id"]
    assert status_of(client, job_id) == "pending"
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 200
    wait_status(client, job_id, "awaiting_review")


def test_upload_rejects_bad_extension(client, tmp_path):
    res = post_video(client, "notes.txt")
    assert res.status_code == 400
    assert "Unsupported file type" in res.json()["detail"]
    assert client.get("/api/jobs").json() == []
    assert not (tmp_path / "input" / "notes.txt").exists()


def test_upload_without_file_is_422(client):
    assert client.post("/api/jobs").status_code == 422


def test_upload_filename_with_path_is_flattened(client, tmp_path):
    res = post_video(client, "../../x/clip.mkv")
    assert res.status_code == 200
    assert (tmp_path / "input" / "clip.mkv").is_file()
    assert not (tmp_path.parent / "x").exists()


def test_segments_endpoint(client):
    job_id = job_in_review(client)
    segments = client.get(f"/api/jobs/{job_id}/segments").json()
    assert segments == [
        {
            "index": 0,
            "start": 0.0,
            "end": 1.5,
            "text_zh": "你好",
            "text_vi": "Xin chao",
            "flags": [],
        },
        {
            "index": 1,
            "start": 1.5,
            "end": 3.0,
            "text_zh": "谢谢",
            "text_vi": "Cam on",
            "flags": ["too_long"],
        },
    ]


def test_edit_validation(client):
    job_id = job_in_review(client)
    url = f"/api/jobs/{job_id}/segments"
    for bad in (
        {"edits": [{"index": 9, "text_vi": "x"}]},
        {"edits": [{"index": -1, "text_vi": "x"}]},
        {"edits": [{"index": 0, "text_vi": "   "}]},
        {"edits": []},
        {"edits": {"abc": "x"}},
        {"edits": [{"index": 0, "text_vi": "a"}, {"index": 0, "text_vi": "b"}]},
    ):
        res = client.put(url, json=bad)
        assert res.status_code == 422, bad
    assert client.put(url, json={"nonsense": 1}).status_code == 422
    assert client.get(url).json()[0]["text_vi"] == "Xin chao"


def test_edit_list_and_dict_forms(client):
    job_id = job_in_review(client)
    url = f"/api/jobs/{job_id}/segments"
    assert client.put(url, json={"edits": [{"index": 0, "text_vi": "Chao ban"}]}).status_code == 200
    assert client.put(url, json={"edits": {"1": "Cam on nhieu"}}).status_code == 200
    texts = [s["text_vi"] for s in client.get(url).json()]
    assert texts == ["Chao ban", "Cam on nhieu"]


def test_edit_not_allowed_outside_review(client):
    job_id = post_video(client, start="false").json()["id"]
    res = client.put(f"/api/jobs/{job_id}/segments", json={"edits": [{"index": 0, "text_vi": "x"}]})
    assert res.status_code == 409


def test_approve_then_done_and_downloads_with_chinese_name(client):
    job_id = job_in_review(client, "测试视频.mp4")
    assert client.get(f"/api/jobs/{job_id}/download/video").status_code == 404  # not ready
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 409  # must approve
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    wait_status(client, job_id, "done")
    info = client.get(f"/api/jobs/{job_id}").json()
    assert info["downloads"] == {"video": True, "srt": True}
    assert info["progress"]["fraction"] == 1.0

    video = client.get(f"/api/jobs/{job_id}/download/video")
    assert video.status_code == 200
    assert video.content == b"video-bytes"
    assert "filename*=utf-8''" in video.headers["content-disposition"].lower()
    assert "%E6%B5%8B%E8%AF%95%E8%A7%86%E9%A2%91_vi.mp4" in video.headers["content-disposition"]

    srt = client.get(f"/api/jobs/{job_id}/download/srt")
    assert srt.status_code == 200
    assert "Xin chao" in srt.content.decode("utf-8")
    assert "_vi.srt" in srt.headers["content-disposition"]

    assert client.get(f"/api/jobs/{job_id}/download/other").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 409


def test_srt_prefers_srt_output_artifact(client, tmp_path):
    job_id = job_in_review(client)
    manager = client.app.state.jobs
    job = manager.get(job_id)
    special = job.work_dir / "special.srt"
    special.write_text("special", encoding="utf-8")
    job.artifacts["srt_output"] = special
    job.save()
    assert client.get(f"/api/jobs/{job_id}/download/srt").content == b"special"


def test_download_refuses_files_outside_work_and_output(client, tmp_path):
    job_id = job_in_review(client)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    job = client.app.state.jobs.get(job_id)
    job.artifacts["output"] = secret
    job.save()
    assert client.get(f"/api/jobs/{job_id}/download/video").status_code == 404


@pytest.mark.parametrize(
    "bad_id", ["..", "not-an-id", "ABCDEF123456", "0123456789a", "0123456789abc", "%2e%2e"]
)
def test_invalid_job_ids_rejected(client, bad_id):
    for suffix in ("", "/segments", "/download/video"):
        assert client.get(f"/api/jobs/{bad_id}{suffix}").status_code == 404
    assert client.post(f"/api/jobs/{bad_id}/approve").status_code == 404
    assert client.post(f"/api/jobs/{bad_id}/resume").status_code == 404
    assert client.put(f"/api/jobs/{bad_id}/segments", json={"edits": {"0": "x"}}).status_code == 404


def test_unknown_but_valid_id_is_404(client):
    assert client.get("/api/jobs/0123456789ab").status_code == 404


def test_interrupted_running_job_is_shown_paused(tmp_path):
    with make_client(tmp_path) as first:
        job_id = post_video(first, start="false").json()["id"]
        job = first.app.state.jobs.get(job_id)
        job.status = JobStatus.RUNNING
        job.save()
        first.app.state.jobs.shutdown()
    with make_client(tmp_path) as second:
        info = second.get(f"/api/jobs/{job_id}").json()
        assert info["status"] == "paused"
        assert info["running"] is False
        assert "interrupted" in info["message"].lower()
        assert "Resume" in info["message"]
        assert [j["status"] for j in second.get("/api/jobs").json()] == ["paused"]
        assert second.post(f"/api/jobs/{job_id}/resume").status_code == 200
        wait_status(second, job_id, "awaiting_review")
        second.app.state.jobs.shutdown()


def test_second_job_is_queued_while_one_runs(tmp_path):
    gate = threading.Event()
    with make_client(tmp_path, gate) as client:
        first = post_video(client, "a.mp4").json()["id"]
        second = post_video(client, "b.mp4").json()
        assert second["queued"] is True
        assert second["running"] is False
        wait_until(lambda: client.get(f"/api/jobs/{first}").json()["running"])
        assert client.get(f"/api/jobs/{first}").json()["status"] == "running"
        gate.set()
        wait_status(client, first, "awaiting_review")
        wait_status(client, second["id"], "awaiting_review")
        assert client.get(f"/api/jobs/{second['id']}").json()["queued"] is False
        client.app.state.jobs.shutdown()


def test_doctor_endpoint(client):
    body = client.get("/api/doctor").json()
    names = [c["name"] for c in body["checks"]]
    assert "Python" in names and "ffmpeg" in names
    assert all(set(c) == {"name", "status", "message"} for c in body["checks"])
    assert all(c["status"] in {"OK", "WARN", "FAIL"} for c in body["checks"])
    assert body["ok"] == (doctor.exit_code(doctor.run_checks()) == 0)


def test_doctor_endpoint_reports_failures(client, monkeypatch):
    monkeypatch.setattr(
        doctor, "run_checks", lambda: [doctor.CheckResult("ffmpeg", doctor.FAIL, "missing")]
    )
    body = client.get("/api/doctor").json()
    assert body == {
        "ok": False,
        "checks": [{"name": "ffmpeg", "status": "FAIL", "message": "missing"}],
    }


def test_config_summary_has_no_secrets(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-super-secret")
    res = client.get("/api/config/summary")
    body = res.json()
    assert body["review"] is True
    assert body["chunk_minutes"] == 10
    assert body["providers"] == {"asr": "gemini", "translation": "gemini", "tts": "elevenlabs"}
    assert body["elevenlabs"]["monthly_character_budget"] == 10000
    assert body["keys_set"] == {"gemini": True, "elevenlabs": True}
    assert "super-secret" not in res.text


def test_config_summary_usage_is_none_or_int(client):
    used = client.get("/api/config/summary").json()["elevenlabs"]["characters_used_this_month"]
    assert used is None or isinstance(used, int)


def test_frontend_never_uses_innerhtml():
    web = Path(__file__).resolve().parents[1] / "src" / "video_translator" / "web"
    assert "innerHTML" not in (web / "static" / "app.js").read_text(encoding="utf-8")
    assert "http" not in (web / "templates" / "index.html").read_text(encoding="utf-8").replace(
        "http-equiv", ""
    )


def test_job_object_is_persisted(client):
    job_id = job_in_review(client)
    work = client.app.state.config.paths.work
    assert Job.load(Path(work) / job_id).status == JobStatus.AWAITING_REVIEW
