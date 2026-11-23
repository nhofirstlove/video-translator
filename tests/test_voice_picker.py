"""The voice picker of the upload page: voice list, spoken samples, per-video voice, default."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.e2e_support import FakeVieNeu
from video_translator.config import AppConfig, PathsConfig
from video_translator.models import Job, Segment
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.base import TTSSetupError
from video_translator.providers.tts.vieneu import VieNeuTTS
from video_translator.stages.base import Stage
from video_translator.stages.synthesize import SynthesizeStage
from video_translator.web.app import create_app
from video_translator.web.voice_samples import DEFAULT_SAMPLE_TEXT, MAX_TEXT_CHARS


class Nothing(Stage):
    name = "nothing"

    def run(self, job, config):
        return job


class Env:
    def __init__(self, tmp_path, factory=None, mode="library"):
        self.fake = FakeVieNeu()
        self.config = AppConfig(
            paths=PathsConfig(
                input=tmp_path / "input", work=tmp_path / "work", output=tmp_path / "output"
            )
        )
        self.config.vieneu.voice = "Trúc Ly"
        self.config.vieneu.mode = mode

        def make_tts(cfg):
            return VieNeuTTS(config=cfg, transport=self.fake)

        app = create_app(
            self.config,
            lambda cfg: Pipeline([Nothing()], cfg),
            factory or make_tts,
        )
        self.client = TestClient(app)
        self.manager = app.state.jobs
        self.tmp_path = tmp_path


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    with e.client:
        yield e
        e.manager.shutdown()


def upload(env, **params):
    files = {"file": ("clip.mp4", b"fake video", "video/mp4")}
    return env.client.post("/api/jobs", files=files, params={"start": "false", **params})


# --- the list and the samples ----------------------------------------------------------------


def test_lists_voices_with_default_and_sample_text(env):
    data = env.client.get("/api/voices").json()
    assert [(v["id"], v["label"]) for v in data["voices"]] == [
        ("Minh Quân", "⭐ Minh Quân — Nam · Bắc"),
        ("Mai Anh", "⭐ Mai Anh — Nữ · Bắc · Giọng đọc tự nhiên"),
        ("Trúc Ly", "Trúc Ly — Nữ · Nam"),
    ]
    assert data["default"] == "Trúc Ly"
    assert data["sample_text"] == DEFAULT_SAMPLE_TEXT
    assert data["note"] == ""


def test_sample_speaks_the_chosen_voice_and_text(env):
    res = env.client.post("/api/voices/sample", json={"voice": "Mai Anh", "text": "Chào bạn"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.content[:4] == b"RIFF"
    assert env.fake.calls == [("Chào bạn", "Mai Anh")]


def test_sample_with_a_display_label_still_speaks_the_bare_voice_name(env):
    label = "⭐ Mai Anh — Nữ · Bắc · Giọng đọc tự nhiên"
    res = env.client.post("/api/voices/sample", json={"voice": label, "text": "Chào"})
    assert res.status_code == 200, res.text
    assert env.fake.calls == [("Chào", "Mai Anh")]


def test_sample_is_cached_per_voice_and_text(env):
    body = {"voice": "Mai Anh", "text": "Chào bạn"}
    first = env.client.post("/api/voices/sample", json=body)
    second = env.client.post("/api/voices/sample", json=body)
    assert first.content == second.content
    assert len(env.fake.calls) == 1  # the second one came from the disk
    env.client.post("/api/voices/sample", json={**body, "voice": "Minh Quân"})
    env.client.post("/api/voices/sample", json={**body, "text": "Tạm biệt"})
    assert len(env.fake.calls) == 3


def test_sample_without_voice_or_text_uses_the_defaults(env):
    res = env.client.post("/api/voices/sample", json={"voice": "", "text": "   "})
    assert res.status_code == 200
    assert env.fake.calls == [(DEFAULT_SAMPLE_TEXT, "Trúc Ly")]


def test_sample_refuses_unknown_voice_and_long_text(env):
    res = env.client.post("/api/voices/sample", json={"voice": "Nobody", "text": "a"})
    assert res.status_code == 400 and "Nobody" in res.json()["detail"]
    res = env.client.post("/api/voices/sample", json={"text": "a" * (MAX_TEXT_CHARS + 1)})
    assert res.status_code == 400
    assert env.fake.calls == []


def test_server_mode_cannot_list_voices(tmp_path):
    e = Env(tmp_path, mode="server")
    with e.client:
        data = e.client.get("/api/voices").json()
        assert data["voices"] == [] and "Type a voice name" in data["note"]
        assert data["default"] == "Trúc Ly"
        e.manager.shutdown()


def test_missing_voice_package_is_explained(tmp_path):
    def broken(cfg):
        raise TTSSetupError("The VieNeu-TTS voice package is not installed.")

    e = Env(tmp_path, factory=broken)
    with e.client:
        data = e.client.get("/api/voices").json()
        assert data["voices"] == [] and "not installed" in data["note"]
        res = e.client.post("/api/voices/sample", json={"text": "a"})
        assert res.status_code == 502 and "not installed" in res.json()["detail"]
        e.manager.shutdown()


# --- the voice of a video --------------------------------------------------------------------


def test_upload_stores_the_voice_on_the_job_only(env, tmp_path, monkeypatch):
    base = tmp_path / "cfg" / "default.yaml"
    base.parent.mkdir()
    base.write_text("review: true\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(base))
    data = upload(env, voice="Mai Anh").json()
    assert data["voice"] == "Mai Anh" and "default_voice_saved" not in data
    assert env.manager.get(data["id"]).voice == "Mai Anh"
    assert not (base.parent / "local.yaml").exists()  # this video only


def test_upload_without_voice_uses_the_configured_one(env):
    data = upload(env).json()
    assert data["voice"] == "Trúc Ly"
    assert env.manager.get(data["id"]).voice is None


def test_make_default_saves_the_voice_in_local_yaml(env, tmp_path, monkeypatch):
    base = tmp_path / "cfg" / "default.yaml"
    base.parent.mkdir()
    base.write_text("review: true\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(base))
    data = upload(env, voice="Mai Anh", make_default="true").json()
    assert data["default_voice_saved"] is True
    saved = yaml.safe_load((base.parent / "local.yaml").read_text(encoding="utf-8"))
    assert saved == {"vieneu": {"voice": "Mai Anh"}}
    assert env.manager.get(data["id"]).voice == "Mai Anh"


def test_make_default_problem_does_not_lose_the_upload(env, tmp_path, monkeypatch):
    monkeypatch.setenv("VT_CONFIG", str(tmp_path / "missing" / "default.yaml"))
    monkeypatch.chdir(tmp_path)
    data = upload(env, voice="Mai Anh", make_default="true").json()
    assert data["default_voice_saved"] is False and data["default_voice_error"]
    assert env.manager.get(data["id"]).voice == "Mai Anh"


@pytest.mark.parametrize("bad", ["x" * 200, "a\nb"])
def test_upload_refuses_invalid_voice_names(env, bad):
    assert upload(env, voice=bad).status_code == 400
    assert env.manager.list_jobs() == []


def test_synthesize_stage_uses_the_job_voice(tmp_path):
    cfg = AppConfig()
    cfg.vieneu.voice = "Trúc Ly"
    fake = FakeVieNeu()
    stage = SynthesizeStage(provider=VieNeuTTS(config=cfg, transport=fake))
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    job.work_dir.mkdir()
    job.segments = [Segment(start=0.0, end=2.0, text_vi="một hai")]
    job.voice = "Mai Anh"
    stage.run(job, cfg)
    assert [voice for _, voice in fake.calls] == ["Mai Anh"]

    job.segments[0].audio_path = None
    job.voice = None
    stage.run(job, cfg)
    assert [voice for _, voice in fake.calls] == ["Mai Anh", "Trúc Ly"]


def test_single_line_regeneration_uses_the_job_voice(tmp_path, monkeypatch):
    import video_translator.synth_line as synth_line

    monkeypatch.setattr(synth_line, "fit_segment", lambda job, index: None)
    cfg = AppConfig()
    cfg.vieneu.voice = "Trúc Ly"
    fake = FakeVieNeu()
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    job.work_dir.mkdir()
    job.segments = [Segment(start=0.0, end=2.0, text_vi="một hai")]
    job.voice = "Minh Quân"
    synth_line.synthesize_line(job, 0, cfg, lambda c: VieNeuTTS(config=c, transport=fake))
    assert [voice for _, voice in fake.calls] == ["Minh Quân"]


def test_picker_is_on_the_upload_page_only():
    from video_translator.web.app import WEB_DIR

    index = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    editor = (WEB_DIR / "templates" / "editor.html").read_text(encoding="utf-8")
    assert 'id="voice-picker"' in index and 'id="voice-default"' in index
    assert "voice-picker" not in editor
