"""Voice separation (Demucs) as a per-video setting: provider, stage and switches."""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

import pytest

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job
from video_translator.providers.separation.demucs import (
    DemucsSeparation,
    SeparationError,
    load_backend,
)
from video_translator.stages.separate_audio import SeparateAudioStage
from video_translator.utils.ffmpeg import probe_duration

RATE = 44100


def write_stereo(path: Path, seconds: float, freq: float = 440.0) -> Path:
    n = int(seconds * RATE)
    frames = b"".join(
        struct.pack("<hh", v, v)
        for v in (int(8000 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    return path


class FakeBackend:
    """Calls the first half of every slice 'voice' and the rest 'background'."""

    samplerate = RATE

    def __init__(self) -> None:
        self.calls = 0

    def split(self, wav):
        import numpy as np

        self.calls += 1
        half = wav.shape[1] // 2
        voice = np.zeros_like(wav)
        voice[:, :half] = wav[:, :half]
        return voice, wav - voice


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def test_provider_joins_slices_and_keeps_the_length(tmp_path):
    audio = write_stereo(tmp_path / "audio.wav", 5.0)
    seen = []
    p = DemucsSeparation(
        backend=FakeBackend(), slice_seconds=2.0, progress=lambda d, t: seen.append((d, t))
    )
    vocals, background = p.separate(audio, tmp_path / "sep")
    assert abs(duration(vocals) - 5.0) < 0.01 and abs(duration(background) - 5.0) < 0.01
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert not list((tmp_path / "sep").glob("vocals_*.wav"))  # slices are cleaned up


def test_provider_resumes_finished_slices_and_reuses_a_finished_result(tmp_path):
    audio = write_stereo(tmp_path / "audio.wav", 5.0)
    first = FakeBackend()
    DemucsSeparation(backend=first, slice_seconds=2.0).separate(audio, tmp_path / "sep")
    assert first.calls == 3
    again = FakeBackend()
    DemucsSeparation(backend=again, slice_seconds=2.0).separate(audio, tmp_path / "sep")
    assert again.calls == 0  # the finished files are reused

    class Breaks(FakeBackend):
        def split(self, wav):
            if self.calls == 2:
                raise RuntimeError("stopped")
            return super().split(wav)

    out = tmp_path / "sep2"
    with pytest.raises(RuntimeError):
        DemucsSeparation(backend=Breaks(), slice_seconds=2.0).separate(audio, out)
    resumed = FakeBackend()
    DemucsSeparation(backend=resumed, slice_seconds=2.0).separate(audio, out)
    assert resumed.calls == 1  # only the slice that had not finished


def test_provider_rejects_a_model_with_another_sample_rate(tmp_path):
    audio = write_stereo(tmp_path / "audio.wav", 1.0)
    backend = FakeBackend()
    backend.samplerate = 32000
    with pytest.raises(SeparationError, match="32000"):
        DemucsSeparation(backend=backend).separate(audio, tmp_path / "sep")


def test_missing_demucs_package_gives_install_steps(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_demucs(name, *args, **kwargs):
        if name.split(".")[0] in {"demucs", "torch"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_demucs)
    with pytest.raises(SeparationError, match="pip install demucs"):
        load_backend("htdemucs", "cpu")


def make_job(tmp_path, separate: bool) -> Job:
    job = Job.create(tmp_path / "v.mp4", tmp_path / "work")
    job.separate_voice = separate
    audio = write_stereo(job.work_dir / "audio" / "audio.wav", 3.0)
    job.artifacts["audio"] = audio
    chunk = job.work_dir / "chunks" / "chunk_000.wav"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"original")
    job.chunks = [Chunk(index=0, start=0.0, end=3.0, path=chunk)]
    return job


def test_stage_passes_through_when_the_job_did_not_ask(tmp_path):
    job = make_job(tmp_path, False)
    before = list(job.chunks)
    out = SeparateAudioStage(provider=DemucsSeparation(backend=FakeBackend())).run(job, AppConfig())
    assert out.chunks == before and "vocals" not in out.artifacts


def test_stage_recuts_the_chunks_from_the_voice_only(tmp_path):
    job = make_job(tmp_path, True)
    original = job.chunks[0].path
    provider = DemucsSeparation(backend=FakeBackend(), slice_seconds=2.0)
    out = SeparateAudioStage(provider=provider).run(job, AppConfig())
    assert {"vocals", "background"} <= set(out.artifacts)
    (chunk,) = out.chunks
    assert chunk.path != original and chunk.path.parent.name == "chunks_vocals"
    assert (chunk.start, chunk.end) == (0.0, 3.0)
    assert abs(probe_duration(chunk.path) - 3.0) < 0.1
    assert original.read_bytes() == b"original"  # the original chunk is untouched
    assert out.artifacts["audio"].name == "audio.wav"  # the mix still has the original sound


def test_job_switch_defaults_and_web_cli_wiring(tmp_path):
    from fastapi.testclient import TestClient

    from video_translator.web.app import create_app

    assert Job.create(tmp_path / "a.mp4", tmp_path / "w").separate_voice is False
    cfg = AppConfig()
    cfg.paths.work = str(tmp_path / "work")
    cfg.paths.input = str(tmp_path / "input")
    client = TestClient(create_app(cfg, pipeline_factory=lambda c: _Idle()))
    on = client.post(
        "/api/jobs?start=false&separate_voice=true", files={"file": ("a.mp4", io.BytesIO(b"x"))}
    ).json()
    off = client.post("/api/jobs?start=false", files={"file": ("b.mp4", io.BytesIO(b"x"))}).json()
    assert on["separate_voice"] is True and off["separate_voice"] is False
    cfg.separation.enabled = True
    default_on = client.post(
        "/api/jobs?start=false", files={"file": ("c.mp4", io.BytesIO(b"x"))}
    ).json()
    assert default_on["separate_voice"] is True
    assert client.get("/api/config/summary").json()["separate_voice"] is True
    html = client.get("/").text
    assert 'id="separate-voice"' in html


class _Idle:
    def resume(self, job, on_progress=None):
        return job


def test_cli_flag_and_defaults():
    from video_translator import cli

    args = cli.build_parser().parse_args(["run", "v.mp4", "--separate-voice"])
    assert args.separate_voice is True
    assert cli.build_parser().parse_args(["run", "v.mp4"]).separate_voice is False
    cfg = AppConfig()
    assert cfg.separation.enabled is False and cfg.separation.model == "htdemucs"


def test_beeps_are_looked_for_in_the_original_chunk(tmp_path):
    from video_translator.providers.asr.whisper_only import WhisperOnlyASR
    from video_translator.providers.asr.whisper_timing import TimedLine, Word
    from video_translator.stages.transcribe import TranscribeStage

    class Transport:
        def transcribe(self, audio_path, language):
            return [TimedLine(0.0, 1.0, "你好", [Word(0.0, 1.0, "你好")])]

    job = make_job(tmp_path, True)
    job = SeparateAudioStage(
        provider=DemucsSeparation(backend=FakeBackend(), slice_seconds=2.0)
    ).run(job, AppConfig())
    asr = WhisperOnlyASR(whisper_transport=Transport())
    looked_in: list[Path] = []
    asr.timer._beep_finder = lambda path: looked_in.append(path) or []
    TranscribeStage(provider=asr).run(job, AppConfig())
    assert looked_in == [tmp_path / "work" / job.id / "chunks" / "chunk_000.wav"]
