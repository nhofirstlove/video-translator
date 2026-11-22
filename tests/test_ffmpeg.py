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


# --- filter graph passed as a file: ffmpeg 8 renamed the option ---------------------------------


def _fake_ffmpeg(monkeypatch, tmp_path, supported: str, *, major: int | None = None):
    """Pretend to be an ffmpeg that knows only one spelling (``supported``) of the option."""
    from video_translator.utils import ffmpeg as ff

    graph = tmp_path / "g.txt"
    graph.write_text("[0:a]anull[out]", encoding="utf-8")
    calls = []

    def fake_run(args, *, desc=""):
        calls.append(list(args))
        used = next(a for a in args if a in (*ff._FILTER_FILE_OPTIONS, "-filter_complex"))
        if used != supported:
            raise ff.FFmpegError(
                f"ffmpeg failed ({desc}) with code 1:\nUnrecognized option '{used.lstrip('-')}'."
            )

    monkeypatch.setattr(ff, "run_ffmpeg", fake_run)
    monkeypatch.setattr(ff, "_working_filter_option", None)
    monkeypatch.setattr(ff, "_detect_filter_option", lambda: 0 if (major or 7) < 8 else 1)
    return ff, calls, graph


def test_parse_ffmpeg_major():
    from video_translator.utils.ffmpeg import parse_ffmpeg_major

    assert parse_ffmpeg_major("ffmpeg version 8.0-full_build-www.gyan.dev Copyright") == 8
    assert parse_ffmpeg_major("ffmpeg version n7.1.1 Copyright") == 7
    assert parse_ffmpeg_major("ffmpeg version 2024-05-01-git-abc") == 2024
    assert parse_ffmpeg_major("something else") is None


def test_filter_file_old_ffmpeg(monkeypatch, tmp_path):
    ff, calls, g = _fake_ffmpeg(monkeypatch, tmp_path, "-filter_complex_script", major=7)
    ff.run_ffmpeg_filter_file(["-i", "a"], g, ["out.wav"])
    assert calls == [["-i", "a", "-filter_complex_script", g, "out.wav"]]


def test_filter_file_ffmpeg8_uses_new_option_straight_away(monkeypatch, tmp_path):
    ff, calls, g = _fake_ffmpeg(monkeypatch, tmp_path, "-/filter_complex", major=8)
    ff.run_ffmpeg_filter_file(["-i", "a"], g, ["out.wav"])
    assert [c[2] for c in calls] == ["-/filter_complex"]


def test_filter_file_wrong_version_guess_retries_and_remembers(monkeypatch, tmp_path):
    ff, calls, g = _fake_ffmpeg(monkeypatch, tmp_path, "-/filter_complex", major=7)
    ff.run_ffmpeg_filter_file(["-i", "a"], g, ["out.wav"])
    assert [c[2] for c in calls] == ["-filter_complex_script", "-/filter_complex"]
    ff.run_ffmpeg_filter_file(["-i", "a"], g, ["out.wav"])
    assert len(calls) == 3 and calls[2][2] == "-/filter_complex"


def test_filter_file_falls_back_to_inline_graph(monkeypatch, tmp_path):
    ff, calls, g = _fake_ffmpeg(monkeypatch, tmp_path, "-filter_complex", major=8)
    ff.run_ffmpeg_filter_file(["-i", "a"], g, ["out.wav"])
    assert calls[-1][2:4] == ["-filter_complex", "[0:a]anull[out]"]


def test_filter_file_other_errors_are_not_retried(monkeypatch, tmp_path):
    import pytest

    from video_translator.utils import ffmpeg as ff

    calls = []

    def broken(args, *, desc=""):
        calls.append(args)
        raise ff.FFmpegError("ffmpeg failed with code 1:\nInvalid filtergraph")

    monkeypatch.setattr(ff, "run_ffmpeg", broken)
    monkeypatch.setattr(ff, "_working_filter_option", 0)
    with pytest.raises(ff.FFmpegError, match="Invalid filtergraph"):
        ff.run_ffmpeg_filter_file([], tmp_path / "g.txt", [])
    assert len(calls) == 1
