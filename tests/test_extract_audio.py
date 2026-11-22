from __future__ import annotations

import shutil

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.extract_audio import ExtractAudioStage, plan_chunks
from video_translator.utils.ffmpeg import FFmpegError, probe_duration, run_ffmpeg

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

VIDEO_SECONDS = 27


def make_video(path, *, audio=True, seconds=VIDEO_SECONDS, streams=1):
    args = ["-f", "lavfi", "-i", f"testsrc=size=128x96:rate=10:duration={seconds}"]
    if audio:
        for i in range(streams):
            args += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}:duration={seconds}"]
    args += ["-map", "0:v"]
    if audio:
        for i in range(streams):
            args += ["-map", f"{i + 1}:a"]
    args += ["-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
    run_ffmpeg(args, desc="make video")
    return path


def small_chunk_config(tmp_path, minutes=10 / 60):
    # chunk_minutes only accepts whole numbers when validated; model_copy skips validation.
    return AppConfig().model_copy(update={"chunk_minutes": minutes})


def test_plan_chunks_short_video_is_one_chunk():
    assert plan_chunks(30.0, 600, 5.0) == [(0, 30.0)]
    assert plan_chunks(603.0, 600, 5.0) == [(0, 603.0)]


def test_plan_chunks_overlap_and_true_end():
    assert plan_chunks(1500.0, 600, 5.0) == [(0, 605.0), (600, 1205.0), (1200, 1500.0)]


@needs_ffmpeg
def test_extract_and_chunk(tmp_path):
    video = make_video(tmp_path / "视频 video.mp4")
    job = Job.create(video, tmp_path / "work")
    job = ExtractAudioStage().run(job, small_chunk_config(tmp_path))  # 10 s chunks

    audio = job.artifacts["audio"]
    assert audio == job.work_dir / "audio" / "audio.wav"
    assert probe_duration(audio) == pytest.approx(VIDEO_SECONDS, abs=0.3)

    # 10 s chunks starting at 0, 10, 20; each runs 5 s into the next; the last ends at the end
    total = probe_duration(audio)
    assert [(c.index, c.start) for c in job.chunks] == [(0, 0), (1, 10.0), (2, 20.0)]
    assert [c.end for c in job.chunks] == [15.0, 25.0, pytest.approx(total, abs=0.01)]
    assert job.chunks[0].path == job.work_dir / "chunks" / "chunk_000.wav"
    for chunk in job.chunks:
        assert probe_duration(chunk.path) == pytest.approx(chunk.end - chunk.start, abs=0.1)
    assert job.chunks[0].end - job.chunks[1].start == pytest.approx(5.0)


@needs_ffmpeg
def test_output_formats(tmp_path):
    from video_translator.utils.ffmpeg import _probe

    video = make_video(tmp_path / "v.mp4")
    job = ExtractAudioStage().run(Job.create(video, tmp_path / "w"), small_chunk_config(tmp_path))
    full = _probe(job.artifacts["audio"], "-show_entries", "stream=sample_rate,channels,codec_name")
    chunk = _probe(job.chunks[0].path, "-show_entries", "stream=sample_rate,channels,codec_name")
    assert full["streams"][0] == {
        "codec_name": "pcm_s16le", "sample_rate": "44100", "channels": 2,
    }  # fmt: skip
    assert chunk["streams"][0] == {
        "codec_name": "pcm_s16le", "sample_rate": "16000", "channels": 1,
    }  # fmt: skip


@needs_ffmpeg
def test_short_video_is_one_chunk(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=3)
    job = ExtractAudioStage().run(Job.create(video, tmp_path / "w"), AppConfig())
    assert len(job.chunks) == 1
    assert job.chunks[0].start == 0
    assert job.chunks[0].end == pytest.approx(3, abs=0.2)


@needs_ffmpeg
def test_first_audio_stream_is_used(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=3, streams=2)
    job = ExtractAudioStage().run(Job.create(video, tmp_path / "w"), AppConfig())
    assert probe_duration(job.artifacts["audio"]) == pytest.approx(3, abs=0.2)


@needs_ffmpeg
def test_resume_skips_existing_files(tmp_path):
    video = make_video(tmp_path / "v.mp4")
    config = small_chunk_config(tmp_path)
    job = ExtractAudioStage().run(Job.create(video, tmp_path / "w"), config)
    marker = b"RESUME-MARKER"
    paths = [job.artifacts["audio"], *(c.path for c in job.chunks)]
    stats = {}
    for p in paths:
        # keep the audio decodable: only mark the chunks, check the audio by mtime
        stats[p] = p.stat().st_mtime_ns
    job.chunks[0].path.write_bytes(marker)

    fresh = Job(id=job.id, input_path=video, work_dir=job.work_dir)
    again = ExtractAudioStage().run(fresh, config)
    assert again.chunks[0].path.read_bytes() == marker  # not regenerated
    assert again.artifacts["audio"].stat().st_mtime_ns == stats[again.artifacts["audio"]]
    assert [c.index for c in again.chunks] == [0, 1, 2]

    # an empty file counts as unfinished and is redone
    again.chunks[0].path.write_bytes(b"")
    third = ExtractAudioStage().run(Job(id=job.id, input_path=video, work_dir=job.work_dir), config)
    assert third.chunks[0].path.stat().st_size > 100


@needs_ffmpeg
def test_no_audio_stream_raises(tmp_path):
    video = make_video(tmp_path / "silent.mp4", audio=False, seconds=2)
    with pytest.raises(FFmpegError, match="no audio"):
        ExtractAudioStage().run(Job.create(video, tmp_path / "w"), AppConfig())


def test_missing_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ExtractAudioStage().run(Job.create(tmp_path / "nope.mp4", tmp_path / "w"), AppConfig())
