import array
import math
import shutil
import time
import wave
from pathlib import Path

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, Segment
from video_translator.stages.align import AlignStage, atempo_chain, build_voice_track, plan_clip
from video_translator.utils.ffmpeg import probe_duration

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RATE = 48000


def write_tone(path: Path, seconds: float, freq: float = 440.0, rate: int = RATE, amp=0.5):
    n = int(seconds * rate)
    samples = array.array(
        "h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def read_samples(path: Path):
    with wave.open(str(path), "rb") as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a, w.getframerate()


def active_span(path: Path, threshold=2000):
    """(first, last) time in seconds where |sample| exceeds the threshold."""
    samples, rate = read_samples(path)
    idx = [i for i, v in enumerate(samples) if abs(v) > threshold]
    return idx[0] / rate, idx[-1] / rate


def make_job(tmp_path, specs, total):
    """specs: list of (start, end, clip_seconds)."""
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    (job.work_dir / "tts").mkdir(parents=True)
    write_tone(job.work_dir / "full.wav", total, rate=44100, amp=0.1)
    job.artifacts["audio"] = job.work_dir / "full.wav"
    for i, (start, end, clip) in enumerate(specs):
        path = write_tone(job.work_dir / "tts" / f"seg_{i:05d}.wav", clip, amp=0.5)
        job.segments.append(Segment(start=start, end=end, text_vi="x", audio_path=path))
    return job


def test_atempo_chain_and_plan():
    assert atempo_chain(1.0) == []
    assert atempo_chain(1.3) == ["atempo=1.30000"]
    assert atempo_chain(3.0) == ["atempo=2.0", "atempo=1.50000"]
    assert atempo_chain(0.3)[0] == "atempo=0.5"
    # fits: unchanged; short: slowed to fill the slot (0.85 at most); light overrun allowed;
    # normal speed-up 1.2; rushed 1.35; beyond that: flag
    assert plan_clip(1.8, 2.0, 2.0, 5.0) == (1.0, [], None)
    assert plan_clip(0.5, 2.0, 2.0, 5.0)[0] == 0.85
    assert plan_clip(1.7, 2.0, 2.0, 5.0)[0] == pytest.approx(0.85)
    assert plan_clip(1.25, 1.0, 5.0, 9.0) == (1.0, [], None)  # 0.25 s past the slot is fine
    assert plan_clip(1.5, 1.0, 5.0, 9.0)[0] == pytest.approx(1.5 / 1.3)  # window is 1.3 s
    tempo, flags, cut = plan_clip(1.6, 1.0, 1.4, 5.0)  # normal 1.2 is enough: 1.33 > 1.3 -> rushed
    assert (round(tempo, 3), flags, cut) == (1.231, [], None)
    tempo, flags, cut = plan_clip(3.0, 1.0, 1.4, 5.0)
    assert (tempo, flags, cut) == (1.35, ["too_long"], None)
    tempo, flags, cut = plan_clip(3.0, 1.0, 1.4, 1.5)
    assert flags == ["too_long", "truncated"] and cut == 1.5


def test_speedup_within_gap_no_flag_and_timeline_positions(tmp_path):
    job = make_job(
        tmp_path,
        [
            (1.0, 3.0, 1.9),  # fits: untouched
            (4.0, 5.0, 0.4),  # short: slowed to 0.85x
            (8.0, 9.0, 1.32),  # next at 9.2: available 1.1 -> speed up 1.2x
            (9.2, 10.0, 1.1),  # last: slot 0.8 s + 0.3 s overrun, untouched
        ],
        total=12.0,
    )
    AlignStage().run(job, AppConfig())
    segs = job.segments
    assert all(s.flags == [] for s in segs)
    durations = [probe_duration(s.aligned_path) for s in segs]
    assert durations[0] == pytest.approx(1.9, abs=0.03)
    assert durations[1] == pytest.approx(0.4 / 0.85, abs=0.03)
    assert durations[2] == pytest.approx(1.1, abs=0.03)
    assert durations[3] == pytest.approx(1.1, abs=0.03)
    track = job.artifacts["voice_track"]
    assert track == job.work_dir / "voice_track.wav"
    assert probe_duration(track) == pytest.approx(12.0, abs=0.01)
    samples, rate = read_samples(track)
    assert rate == RATE
    for seg in segs:
        i0 = int(seg.start * RATE)
        win = samples[i0 + 2000 : i0 + 6000]
        assert max(abs(v) for v in win) > 10000  # speech starts at its start time
        before = samples[max(i0 - 3000, 0) : max(i0 - 100, 0)]
        assert all(abs(v) < 100 for v in before)  # and not earlier
    first, last = active_span(track)
    assert first == pytest.approx(1.0, abs=0.02)
    assert last == pytest.approx(9.2 + 1.1, abs=0.05)
    assert job.message.startswith("Aligned 4 lines")


def test_too_long_flag_without_truncation(tmp_path):
    job = make_job(tmp_path, [(0.0, 1.0, 2.5), (2.0, 3.0, 0.9)], total=5.0)
    AlignStage().run(job, AppConfig())
    assert job.segments[0].flags == ["too_long"]
    assert job.segments[1].flags == []
    # speech was rushed 1.35x (not cut): ~1.85 s, ending before the next line at 2.0
    assert probe_duration(job.segments[0].aligned_path) == pytest.approx(2.5 / 1.35, abs=0.03)
    assert "too long" in job.message


def test_truncation_when_overlapping_next_clip(tmp_path):
    job = make_job(tmp_path, [(0.0, 1.0, 3.0), (1.5, 2.5, 0.9)], total=4.0)
    AlignStage().run(job, AppConfig())
    assert job.segments[0].flags == ["too_long", "truncated"]
    assert probe_duration(job.segments[0].aligned_path) == pytest.approx(1.5, abs=0.03)
    samples, _ = read_samples(job.segments[0].aligned_path)
    assert abs(samples[-1]) < 500  # faded out, no click
    assert "cut" in job.message


def test_align_is_resumable(tmp_path):
    job = make_job(tmp_path, [(0.0, 1.0, 0.8)], total=2.0)
    AlignStage().run(job, AppConfig())
    first = job.segments[0].aligned_path
    mtime = first.stat().st_mtime_ns
    AlignStage().run(job, AppConfig())
    assert first.stat().st_mtime_ns == mtime


def test_build_voice_track_sums_overlap(tmp_path):
    a = write_tone(tmp_path / "a.wav", 1.0, amp=0.2)
    b = write_tone(tmp_path / "b.wav", 1.0, amp=0.2)
    out = build_voice_track([(0.5, b), (0.0, a)], 3.0, tmp_path / "t.wav")
    samples, _ = read_samples(out)
    assert len(samples) == 3 * RATE
    assert max(abs(v) for v in samples[: RATE // 2]) < 7000
    assert max(abs(v) for v in samples[int(0.6 * RATE) : int(0.9 * RATE)]) > 7000
    assert max(abs(v) for v in samples[2 * RATE :]) == 0


def test_300_segments_performance(tmp_path):
    specs = [(i * 2.0, i * 2.0 + 1.5, 1.0 + (i % 5) * 0.3) for i in range(300)]
    job = make_job(tmp_path, specs, total=605.0)
    began = time.time()
    AlignStage().run(job, AppConfig())
    elapsed = time.time() - began
    assert elapsed < 90, f"align took {elapsed:.0f}s"
    assert probe_duration(job.artifacts["voice_track"]) == pytest.approx(605.0, abs=0.05)
    assert all(s.aligned_path.is_file() for s in job.segments)
