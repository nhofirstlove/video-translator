import io
import math
import struct
import wave

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.base import TTSError
from video_translator.providers.tts.vieneu import VieNeuTTS
from video_translator.stages.synthesize import SynthesizeStage


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
    def __init__(self, fail_on=None):
        self.calls = []
        self.voices = []
        self.fail_on = fail_on
        self.audio = tone_wav()

    def synthesize(self, text, voice, out_path):
        if self.fail_on is not None and text == self.fail_on:
            raise TTSError("engine broke")
        self.calls.append(text)
        self.voices.append(voice)
        out_path.write_bytes(self.audio)

    def list_voices(self):
        return []


@pytest.fixture
def setup(tmp_path):
    cfg = AppConfig()
    cfg.vieneu.voice = "Mai Anh"
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    job.work_dir.mkdir()
    return cfg, job


def make_stage(cfg, transport):
    return SynthesizeStage(provider=VieNeuTTS(config=cfg, transport=transport))


def segs(texts):
    return [Segment(start=i * 2.0, end=i * 2.0 + 1.5, text_vi=t) for i, t in enumerate(texts)]


def test_synthesizes_each_nonempty_segment_with_the_configured_voice(setup):
    cfg, job = setup
    job.segments = segs(["một", "", "hai ba"])
    transport = FakeTransport()
    make_stage(cfg, transport).run(job, cfg)
    assert transport.calls == ["một", "hai ba"]
    assert set(transport.voices) == {"Mai Anh"}
    assert job.segments[1].audio_path is None
    assert job.segments[0].audio_path == job.work_dir / "tts" / "seg_00000.wav"
    assert job.segments[2].audio_path == job.work_dir / "tts" / "seg_00002.wav"
    assert all(job.segments[i].audio_path.is_file() for i in (0, 2))


def test_existing_clips_are_not_made_again(setup):
    cfg, job = setup
    job.segments = segs(["a", "bb", "ccc"])
    transport = FakeTransport()
    stage = make_stage(cfg, transport)
    stage.run(job, cfg)
    stage.run(job, cfg)
    assert len(transport.calls) == 3
    job.segments[1].audio_path.unlink()
    stage.run(job, cfg)
    assert len(transport.calls) == 4


def test_provider_error_names_the_line_and_keeps_progress(setup):
    cfg, job = setup
    job.segments = segs(["a", "b", "c", "d"])
    transport = FakeTransport(fail_on="c")
    stage = make_stage(cfg, transport)
    with pytest.raises(TTSError, match="line 3") as info:
        stage.run(job, cfg)
    assert "engine broke" in str(info.value)
    assert [s.audio_path is not None for s in job.segments] == [True, True, False, False]
    assert Job.load(job.work_dir).segments[0].audio_path is not None  # saved to disk
    # fixed: only the remaining two lines are spoken
    transport.fail_on = None
    stage.run(job, cfg)
    assert transport.calls == ["a", "b", "c", "d"]


def test_saves_progress_every_ten_segments(setup, monkeypatch):
    cfg, job = setup
    job.segments = segs([f"l{i}" for i in range(25)])
    stage = make_stage(cfg, FakeTransport())
    saved = []

    def fake_save(self):
        saved.append(sum(s.audio_path is not None for s in self.segments))

    monkeypatch.setattr(Job, "save", fake_save)
    stage.run(job, cfg)
    assert saved == [10, 20]


def test_provider_error_through_pipeline_fails_the_job_with_the_line(setup):
    cfg, job = setup
    job.segments = segs(["x"])
    stage = make_stage(cfg, FakeTransport(fail_on="x"))
    with pytest.raises(TTSError):
        Pipeline([stage], cfg).run(job)
    assert job.status == JobStatus.FAILED
    assert "line 1" in job.error and "synthesize" not in job.completed_stages
