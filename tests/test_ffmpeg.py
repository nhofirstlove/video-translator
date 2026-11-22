from __future__ import annotations

import shutil

import pytest

from video_translator.utils import ffmpeg
from video_translator.utils.ffmpeg import (
    FFmpegError,
    find_ffmpeg,
    has_audio_stream,
    probe_duration,
    run_ffmpeg,
)

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def test_missing_tool_error_mentions_doctor(monkeypatch):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    with pytest.raises(FFmpegError, match="video-translator doctor"):
        find_ffmpeg()


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / "âm thanh 音频.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", str(path)], desc="tone")
    return path


@needs_ffmpeg
def test_probe_duration_and_audio_stream(tone):
    assert probe_duration(tone) == pytest.approx(1.5, abs=0.1)
    assert has_audio_stream(tone) is True


@needs_ffmpeg
def test_no_audio_stream(tmp_path):
    video = tmp_path / "视频.mp4"
    run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-pix_fmt", "yuv420p", str(video)]
    )
    assert has_audio_stream(video) is False


@needs_ffmpeg
def test_failure_raises_with_stderr(tmp_path):
    with pytest.raises(FFmpegError, match="convert"):
        run_ffmpeg(["-i", str(tmp_path / "missing.wav"), str(tmp_path / "o.wav")], desc="convert")


@needs_ffmpeg
def test_probe_of_missing_file_raises(tmp_path):
    with pytest.raises(FFmpegError):
        probe_duration(tmp_path / "nope.wav")
