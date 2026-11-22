from __future__ import annotations

import shutil

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.render import RenderStage
from video_translator.utils.ffmpeg import _probe, probe_duration, run_ffmpeg

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def streams(path):
    data = _probe(path, "-show_entries", "stream=codec_type,codec_name")
    return {s["codec_type"]: s["codec_name"] for s in data["streams"]}


def make_video(path, seconds=10):
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"testsrc=size=128x96:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        desc="make video",
    )  # fmt: skip


def make_audio(path, seconds):
    run_ffmpeg(
        ["-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}", str(path)], desc="audio"
    )


def make_config(tmp_path):
    config = AppConfig()
    config.paths.output = tmp_path / "out"
    return config


@needs_ffmpeg
def test_render_replaces_audio_and_copies_video(tmp_path):
    video = tmp_path / "视频 演示.mp4"
    make_video(video)
    mixed = tmp_path / "mixed.wav"
    make_audio(mixed, 10)
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n", encoding="utf-8")

    job = Job.create(video, tmp_path / "work")
    job.artifacts["mixed_audio"] = mixed
    job.artifacts["srt"] = srt
    job = RenderStage().run(job, make_config(tmp_path))

    out = job.artifacts["output"]
    assert out == tmp_path / "out" / "视频 演示.vi.mp4"
    assert out.is_file()
    assert streams(out) == {"video": streams(video)["video"], "audio": "aac"}
    assert probe_duration(out) == pytest.approx(probe_duration(video), abs=0.3)
    assert not list((tmp_path / "out").glob("*.part.*"))

    srt_out = job.artifacts["srt_output"]
    assert srt_out == tmp_path / "out" / "视频 演示.vi.srt"
    assert srt_out.read_text(encoding="utf-8") == srt.read_text(encoding="utf-8")


@needs_ffmpeg
def test_short_audio_is_padded_to_video_length(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video, seconds=10)
    mixed = tmp_path / "short.wav"
    make_audio(mixed, 4)
    job = Job.create(video, tmp_path / "work")
    job.artifacts["mixed_audio"] = mixed
    job = RenderStage().run(job, make_config(tmp_path))
    assert probe_duration(job.artifacts["output"]) == pytest.approx(10, abs=0.3)
    assert "srt_output" not in job.artifacts


@needs_ffmpeg
def test_long_audio_is_trimmed_to_video_length(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video, seconds=5)
    mixed = tmp_path / "long.wav"
    make_audio(mixed, 12)
    job = Job.create(video, tmp_path / "work")
    job.artifacts["mixed_audio"] = mixed
    job = RenderStage().run(job, make_config(tmp_path))
    assert probe_duration(job.artifacts["output"]) == pytest.approx(5, abs=0.3)


def test_render_without_mixed_audio_raises(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="mix"):
        RenderStage().run(Job.create(video, tmp_path / "work"), make_config(tmp_path))
