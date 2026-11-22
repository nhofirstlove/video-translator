import io
import math
import struct
import wave

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.elevenlabs import (
    ElevenLabsTTS,
    TTSAPIError,
    TTSQuotaExceededError,
)
from video_translator.stages.base import PauseRequested
from video_translator.stages.synthesize import SynthesizeStage
from video_translator.usage import UsageTracker


def tone_wav(seconds=0.2, freq=440.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n)
            )
        )
    return buf.getvalue()


class FakeTransport:
    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after
        self.audio = tone_wav()

    def convert(self, text, voice_id, model_id):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise TTSQuotaExceededError("quota exceeded")
        self.calls.append(text)
        return self.audio


@pytest.fixture
def setup(tmp_path):
    cfg = AppConfig()
    cfg.elevenlabs.voice = "v"
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    job.work_dir.mkdir()
    return cfg, job


def make_stage(cfg, transport, tmp_path, budget):
    tts = ElevenLabsTTS(config=cfg, transport=transport, sleep=lambda s: None)
    tracker = UsageTracker(tmp_path / "usage.json", budget)
    return SynthesizeStage(provider=tts, tracker=tracker), tracker


def segs(texts):
    return [Segment(start=i * 2.0, end=i * 2.0 + 1.5, text_vi=t) for i, t in enumerate(texts)]


def test_synthesizes_each_nonempty_segment(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["một", "", "hai ba"])
    transport = FakeTransport()
    stage, tracker = make_stage(cfg, transport, tmp_path, 1000)
    stage.run(job, cfg)
    assert transport.calls == ["một", "hai ba"]
    assert job.segments[1].audio_path is None
    assert job.segments[0].audio_path == job.work_dir / "tts" / "seg_00000.mp3"
    assert job.segments[2].audio_path == job.work_dir / "tts" / "seg_00002.mp3"
    assert all(job.segments[i].audio_path.is_file() for i in (0, 2))
    assert tracker.used == len("một") + len("hai ba")


def test_existing_clips_are_not_paid_again(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["a", "bb", "ccc"])
    transport = FakeTransport()
    stage, tracker = make_stage(cfg, transport, tmp_path, 1000)
    stage.run(job, cfg)
    stage.run(job, cfg)
    assert len(transport.calls) == 3
    assert tracker.used == 6
    job.segments[1].audio_path.unlink()
    stage.run(job, cfg)
    assert len(transport.calls) == 4


def test_whole_job_not_fitting_pauses_before_any_call(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["a" * 60, "b" * 60])
    transport = FakeTransport()
    stage, tracker = make_stage(cfg, transport, tmp_path, 100)
    with pytest.raises(PauseRequested) as info:
        stage.run(job, cfg)
    assert info.value.status == JobStatus.PAUSED
    msg = info.value.message
    assert "120" in msg and "100" in msg and "local.yaml" in msg
    assert transport.calls == [] and tracker.used == 0


def test_quota_error_midway_pauses_and_keeps_progress(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["a", "b", "c", "d"])
    transport = FakeTransport(fail_after=2)
    stage, tracker = make_stage(cfg, transport, tmp_path, 1000)
    with pytest.raises(PauseRequested) as info:
        stage.run(job, cfg)
    assert info.value.status == JobStatus.PAUSED
    assert [s.audio_path is not None for s in job.segments] == [True, True, False, False]
    assert tracker.used == 2
    assert Job.load(job.work_dir).segments[0].audio_path is not None  # saved to disk
    # resume after the quota is back: only the remaining two lines are paid
    transport.fail_after = None
    stage.run(job, cfg)
    assert len(transport.calls) == 4 and tracker.used == 4


def test_saves_progress_every_ten_segments(setup, tmp_path, monkeypatch):
    cfg, job = setup
    job.segments = segs([f"l{i}" for i in range(25)])
    transport = FakeTransport()
    stage, _ = make_stage(cfg, transport, tmp_path, 1000)
    saved = []

    def fake_save(self):
        saved.append(sum(s.audio_path is not None for s in self.segments))

    monkeypatch.setattr(Job, "save", fake_save)
    stage.run(job, cfg)
    assert saved == [10, 20]


def test_pause_through_pipeline_sets_message(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["x" * 50])
    stage, _ = make_stage(cfg, FakeTransport(), tmp_path, 10)
    result = Pipeline([stage], cfg).run(job)
    assert result.status == JobStatus.PAUSED
    assert "50" in result.message and "synthesize" not in result.completed_stages


def test_non_quota_api_error_fails_loudly(setup, tmp_path):
    cfg, job = setup
    job.segments = segs(["a"])

    class Bad:
        def convert(self, *a):
            raise TTSAPIError(400, "bad voice")

    stage, _ = make_stage(cfg, Bad(), tmp_path, 100)
    with pytest.raises(Exception, match="400"):
        stage.run(job, cfg)
