"""End-to-end through the web API and the command line, real stages and fake providers."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.e2e_support import (
    UTTERANCES,
    install_fakes,
    make_config,
    make_video,
    needs_ffmpeg,
    probe_streams,
    vietnamese_for,
)
from tests.test_e2e import EDITED, EDITED_INDEX, edit_review_file, srt_blocks
from video_translator import cli
from video_translator.models import Job, JobStatus
from video_translator.web.app import create_app


def wait_for(predicate, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


@needs_ffmpeg
def test_web_upload_review_approve_download(tmp_path, monkeypatch):
    fakes = install_fakes(monkeypatch, tmp_path)
    config = make_config(tmp_path)
    video = make_video(tmp_path / "clip.mp4")

    with TestClient(create_app(config)) as client:  # the REAL stages (default pipeline factory)
        try:
            with video.open("rb") as handle:
                created = client.post("/api/jobs", files={"file": ("clip.mp4", handle)})
            assert created.status_code == 200, created.text
            job_id = created.json()["id"]

            def state():
                return client.get(f"/api/jobs/{job_id}").json()

            wait_for(lambda: state()["status"] in {"awaiting_review", "failed", "paused"})
            info = state()
            assert info["status"] == "awaiting_review", info
            assert info["segments"] == len(UTTERANCES)
            assert info["downloads"] == {"video": False, "srt": False}
            assert info["progress"]["total"] == 9

            segments = client.get(f"/api/jobs/{job_id}/segments").json()
            assert [s["text_vi"] for s in segments] == [vietnamese_for(u[2]) for u in UTTERANCES]
            assert fakes.tts.calls == []  # nothing is spoken before the review is approved

            edit = client.put(
                f"/api/jobs/{job_id}/segments",
                json={"edits": [{"index": EDITED_INDEX, "text_vi": EDITED}]},
            )
            assert edit.status_code == 200, edit.text
            assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200

            wait_for(lambda: state()["status"] in {"done", "failed", "paused"})
            info = state()
            assert info["status"] == "done", info
            assert info["downloads"] == {"video": True, "srt": True}

            mp4 = client.get(f"/api/jobs/{job_id}/download/video")
            srt = client.get(f"/api/jobs/{job_id}/download/srt")
            assert mp4.status_code == 200 and srt.status_code == 200
            assert "clip_vi.mp4" in mp4.headers["content-disposition"]
            out_file = tmp_path / "downloaded.mp4"
            out_file.write_bytes(mp4.content)
            streams = probe_streams(out_file)
            assert streams["video"]["codec_name"] == "h264"
            assert abs(streams["audio"]["duration"] - streams["video"]["duration"]) < 0.15
            srt_file = tmp_path / "downloaded.srt"
            srt_file.write_bytes(srt.content)
            assert srt_blocks(srt_file)[EDITED_INDEX][1] == EDITED

            assert len(fakes.tts.calls) == len(UTTERANCES)
        finally:
            client.app.state.jobs.shutdown()


@needs_ffmpeg
def test_cli_run_pause_edit_resume_approve(tmp_path, monkeypatch, capsys):
    fakes = install_fakes(monkeypatch, tmp_path)
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    monkeypatch.chdir(tmp_path)
    video = make_video(tmp_path / "clip.mp4")

    assert cli.main(["run", str(video)]) == 0
    out = capsys.readouterr().out
    job_dir = Path(next(line for line in out.splitlines() if line.startswith("Job folder:"))[12:])
    assert "Status: awaiting_review" in out
    assert f"Continue with: video-translator resume {job_dir} --approve" in out
    job = Job.load(job_dir)
    assert job.status == JobStatus.AWAITING_REVIEW
    assert not job.review_approved
    assert fakes.tts.calls == []

    edit_review_file(job, EDITED_INDEX, EDITED)
    assert cli.main(["resume", str(job_dir), "--approve"]) == 0
    out = capsys.readouterr().out
    assert "Status: done" in out
    job = Job.load(job_dir)
    assert job.status == JobStatus.DONE
    assert f"Output: {job.artifacts['output']}" in out
    assert Path(job.artifacts["output"]).is_file()
    assert job.segments[EDITED_INDEX].text_vi == EDITED
    assert len(fakes.tts.calls) == len(UTTERANCES)
    assert len(fakes.gemini.asr_calls) == 3  # nothing repeated on resume


@needs_ffmpeg
def test_cli_paused_by_gemini_quota_prints_resume_command_without_approve(
    tmp_path, monkeypatch, capsys
):
    fakes = install_fakes(monkeypatch, tmp_path)
    fakes.gemini.translate_quota_after = 0
    config = make_config(tmp_path, review=False)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    monkeypatch.chdir(tmp_path)
    video = make_video(tmp_path / "clip.mp4")

    assert cli.main(["run", str(video)]) == 0  # a pause is not an error
    out = capsys.readouterr().out
    assert "Status: paused" in out
    job_dir = next(line for line in out.splitlines() if line.startswith("Job folder:"))[12:]
    assert f"Continue with: video-translator resume {job_dir}\n" in out
    assert "--approve" not in out


@needs_ffmpeg
def test_cli_voice_failure_names_the_line_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    fakes = install_fakes(monkeypatch, tmp_path)
    fakes.tts.fail_on = vietnamese_for(UTTERANCES[1][2])
    config = make_config(tmp_path, review=False)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    monkeypatch.chdir(tmp_path)
    video = make_video(tmp_path / "clip.mp4")

    assert cli.main(["run", str(video)]) == 1
    captured = capsys.readouterr()
    assert "line 2" in captured.err
    assert "Failed in stage 'synthesize'" in captured.err
