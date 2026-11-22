import array
import math
import shutil
import time
import wave
from pathlib import Path

import pytest

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job, Segment
from video_translator.stages.mix import (
    MixStage,
    build_graph,
    duck_filters,
    merge_spans,
    mute_filters,
    speech_ranges,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def write_wav(path: Path, seconds, freq=None, rate=48000, channels=1, amp=0.3):
    n = int(seconds * rate)
    if freq is None:
        samples = array.array("h", bytes(2 * n))
    else:
        samples = array.array(
            "h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
        )
    if channels == 2:
        stereo = array.array("h")
        for v in samples:
            stereo.extend((v, v))
        samples = stereo
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def rms(path: Path, t0: float, t1: float, channel=0):
    with wave.open(str(path), "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        assert (ch, rate) == (2, 48000)
        w.setpos(int(t0 * rate))
        a = array.array("h")
        a.frombytes(w.readframes(int((t1 - t0) * rate)))
    vals = a[channel::ch]
    return math.sqrt(sum(v * v for v in vals) / len(vals))


def make_job(tmp_path, voice_freq=None, total=20.0, speech=((8.0, 12.0),)):
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    job.work_dir.mkdir()
    job.artifacts["audio"] = write_wav(
        job.work_dir / "audio.wav", total, freq=440, rate=44100, channels=2
    )
    voice = job.work_dir / "voice_track.wav"
    n = int(total * 48000)
    samples = array.array("h", bytes(2 * n))
    if voice_freq:
        for a, b in speech:
            for i in range(int(a * 48000), int(b * 48000)):
                samples[i] = int(0.3 * 32767 * math.sin(2 * math.pi * voice_freq * i / 48000))
    with wave.open(str(voice), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.tobytes())
    job.artifacts["voice_track"] = voice
    for i, (a, b) in enumerate(speech):
        clip = write_wav(job.work_dir / f"c{i}.wav", b - a)
        job.segments.append(Segment(start=a, end=b, text_vi="x", aligned_path=clip))
    return job


def test_speech_ranges_merge_close_passages(tmp_path):
    job = make_job(tmp_path, speech=((1.0, 2.0), (2.1, 3.0), (6.0, 7.0)))
    ranges = speech_ranges(job)
    assert ranges == [pytest.approx((1.0, 3.0), abs=1e-3), pytest.approx((6.0, 7.0), abs=1e-3)]


def test_graph_is_batched_and_short_lines(tmp_path):
    ranges = [(i * 3.0, i * 3.0 + 1.0) for i in range(1000)]
    filters = duck_filters(ranges, 0.25)
    assert len(filters) == 25
    assert duck_filters(ranges, 1.0) == [] and duck_filters([], 0.25) == []
    graph = build_graph(ranges, AppConfig())
    assert "amix=inputs=2:duration=first:normalize=0" in graph and "loudnorm" in graph


def test_ducking_lowers_original_during_speech(tmp_path):
    job = make_job(tmp_path)
    MixStage().run(job, AppConfig())
    mixed = job.artifacts["mixed_audio"]
    assert mixed == job.work_dir / "mixed.wav" and mixed.is_file()
    gap = rms(mixed, 2.0, 6.0)
    ducked = rms(mixed, 9.0, 11.0)
    after = rms(mixed, 15.0, 19.0)
    assert ducked / gap == pytest.approx(0.25, rel=0.2)
    assert after / gap == pytest.approx(1.0, rel=0.1)
    assert gap > 1000  # audible after normalisation


def test_ducking_ramps_are_smooth(tmp_path):
    job = make_job(tmp_path)
    MixStage().run(job, AppConfig())
    mixed = job.artifacts["mixed_audio"]
    gap = rms(mixed, 2.0, 6.0)
    mid_ramp = rms(mixed, 7.88, 7.92)  # 0.1 s before speech: ducking is already halfway
    assert 0.3 * gap < mid_ramp < 0.9 * gap
    assert rms(mixed, 7.0, 7.5) == pytest.approx(gap, rel=0.1)


def test_voice_is_added_on_top(tmp_path):
    job = make_job(tmp_path, voice_freq=1000)
    MixStage().run(job, AppConfig())
    mixed = job.artifacts["mixed_audio"]
    gap = rms(mixed, 2.0, 6.0)
    speaking = rms(mixed, 9.0, 11.0)
    assert speaking > 0.5 * gap  # voice clearly present on top of the ducked (0.25) background
    assert rms(mixed, 9.0, 11.0, channel=1) == pytest.approx(speaking, rel=0.05)


def test_voice_volume_and_ducked_volume_from_config(tmp_path):
    job = make_job(tmp_path)
    cfg = AppConfig()
    cfg.audio.ducked_volume = 0.5
    MixStage().run(job, cfg)
    mixed = job.artifacts["mixed_audio"]
    assert rms(mixed, 9.0, 11.0) / rms(mixed, 2.0, 6.0) == pytest.approx(0.5, rel=0.15)


def test_missing_inputs_raise(tmp_path):
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        MixStage().run(job, AppConfig())


def test_300_segments_performance(tmp_path):
    speech = tuple((i * 2.0 + 0.2, i * 2.0 + 1.4) for i in range(300))
    job = make_job(tmp_path, total=600.0, speech=speech)
    began = time.time()
    MixStage().run(job, AppConfig())
    elapsed = time.time() - began
    assert elapsed < 120, f"mix took {elapsed:.0f}s"
    mixed = job.artifacts["mixed_audio"]
    assert rms(mixed, 0.0, 0.1) > rms(mixed, 300.5, 300.9) * 2  # ducked inside speech


# --- bleep tones are silenced in the original ---------------------------------------------------


def make_beep_job(tmp_path):
    """Original = 440 Hz music with a 1480 Hz bleep from 3.0 to 4.0 s (as a 16 kHz chunk too)."""
    job = make_job(tmp_path, total=10.0, speech=((8.0, 9.0),))
    rate = 16000
    n = 10 * rate
    music = [math.sin(2 * math.pi * 440 * i / rate) for i in range(n)]
    for i in range(3 * rate, 4 * rate):
        music[i] = math.sin(2 * math.pi * 1480 * i / rate)
    samples = array.array("h", (int(0.3 * 32767 * v) for v in music))
    chunk = job.work_dir / "chunk_000.wav"
    with wave.open(str(chunk), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    job.chunks.append(Chunk(index=0, start=0.0, end=10.0, path=chunk))
    # the 48 kHz original the mix reads, with the same content
    rate48 = 48000
    orig = array.array("h", bytes(2 * 10 * rate48))
    for i in range(10 * rate48):
        f = 1480 if 3 * rate48 <= i < 4 * rate48 else 440
        orig[i] = int(0.3 * 32767 * math.sin(2 * math.pi * f * i / rate48))
    with wave.open(str(job.artifacts["audio"]), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate48)
        w.writeframes(orig.tobytes())
    return job


def test_mute_filters_and_merge():
    assert mute_filters([]) == []
    (f,) = mute_filters([(3.0, 4.0), (10.0, 10.5)])
    assert f == "volume=0:enable='between(t,2.950,4.050)+between(t,9.950,10.550)'"
    assert merge_spans([(5.0, 6.0), (1.0, 2.0), (2.05, 3.0)]) == [(1.0, 3.0), (5.0, 6.0)]
    assert "volume=0:enable" in build_graph([], AppConfig(), None, [(3.0, 4.0)])
    assert "volume=0:enable" not in build_graph([], AppConfig())


def test_beep_is_silenced_and_music_stays(tmp_path):
    job = make_beep_job(tmp_path)
    MixStage().run(job, AppConfig())
    mixed = job.artifacts["mixed_audio"]
    music = rms(mixed, 1.0, 2.5)
    assert rms(mixed, 3.3, 3.7) < 0.02 * music  # the bleep is gone
    assert rms(mixed, 5.0, 7.0) == pytest.approx(music, rel=0.15)
    assert "1 beeps silenced" in job.message


def test_beep_stays_when_removal_is_turned_off(tmp_path):
    job = make_beep_job(tmp_path)
    cfg = AppConfig()
    cfg.audio.remove_beeps = False
    MixStage().run(job, cfg)
    mixed = job.artifacts["mixed_audio"]
    assert rms(mixed, 3.3, 3.7) > 0.5 * rms(mixed, 1.0, 2.5)
