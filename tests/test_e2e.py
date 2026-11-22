"""End-to-end: the REAL stages and pipeline on a synthetic video, with fake Gemini/ElevenLabs."""

from __future__ import annotations

import os
import re
from pathlib import Path

from tests.e2e_support import (
    BED_FREQS,
    UTTERANCES,
    VOICE_FREQ,
    VOICE_ID,
    install_fakes,
    make_config,
    make_video,
    needs_ffmpeg,
    probe_streams,
    tone_amplitude,
    vietnamese_for,
)
from video_translator.models import Job, JobStatus
from video_translator.pipeline import Pipeline
from video_translator.review_file import review_path
from video_translator.stages import build_stages

EDITED = "Câu này đã được người dùng sửa lại"
EDITED_INDEX = 2


def new_job(tmp_path, config):
    video = tmp_path / "视频.mp4"
    make_video(video)
    return video, Job.create(video, config.paths.work)


def pipeline_for(config):
    return Pipeline(build_stages(config), config)


def edit_review_file(job: Job, index: int, new_text: str) -> None:
    """What the user does in Notepad: change one VI line and save."""
    path = review_path(job)
    text = path.read_bytes().decode("utf-8")
    pattern = re.compile(rf"(#{index}  [^\r\n]*\r?\nZH: [^\r\n]*\r?\nVI: )[^\r\n]*")
    assert pattern.search(text), "segment block not found in the review file"
    path.write_bytes(pattern.sub(lambda m: m.group(1) + new_text, text).encode("utf-8"))
    later = path.stat().st_mtime + 2  # certainly newer than the moment we wrote the file
    os.utime(path, (later, later))


def srt_blocks(path: Path) -> list[tuple[str, str]]:
    blocks = path.read_text(encoding="utf-8").strip().split("\n\n")
    parsed = []
    for block in blocks:
        lines = block.split("\n")
        parsed.append((lines[1], " ".join(lines[2:])))
    return parsed


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@needs_ffmpeg
def test_full_run_with_review_edit_and_resume(tmp_path, monkeypatch):
    fakes = install_fakes(monkeypatch, tmp_path)
    config = make_config(tmp_path)
    video, job = new_job(tmp_path, config)

    # ---- run 1: stops at the review gate --------------------------------------------------
    job = pipeline_for(config).run(job)
    assert job.status == JobStatus.AWAITING_REVIEW
    assert job.completed_stages == ["extract_audio", "transcribe", "translate"]
    assert len(job.chunks) == 3  # 12 s chunks overlapping by 5 s over a 40 s video
    assert len(job.segments) == len(UTTERANCES)  # overlapping chunks were de-duplicated
    for seg, (start, end, zh) in zip(job.segments, UTTERANCES, strict=True):
        assert (seg.start, seg.end, seg.text_zh) == (start, end, zh)  # absolute video times
        assert seg.text_vi == vietnamese_for(zh)
    assert review_path(job).is_file()
    assert "video-translator resume" in job.message
    assert len(fakes.gemini.asr_calls) == 3
    assert len(fakes.gemini.translate_prompts) == 3  # 9 segments in batches of 3
    assert '"context"' in fakes.gemini.translate_prompts[1]  # earlier lines shown for consistency
    assert fakes.tts.calls == []  # nothing paid before the user approved
    assert Job.load(job.work_dir).status == JobStatus.AWAITING_REVIEW

    # ---- the user edits one Vietnamese line and approves ----------------------------------
    edit_review_file(job, EDITED_INDEX, EDITED)
    job = Job.load(job.work_dir)  # a new process would load the job from disk
    job.review_approved = True
    job = pipeline_for(config).resume(job)

    assert job.status == JobStatus.DONE, job.error
    assert job.segments[EDITED_INDEX].text_vi == EDITED
    assert job.completed_stages[-6:] == [
        "review", "subtitles", "synthesize", "align", "mix", "render"
    ]  # fmt: skip

    # no repeated paid calls on resume
    assert len(fakes.gemini.asr_calls) == 3
    assert len(fakes.gemini.translate_prompts) == 3
    assert len(fakes.tts.calls) == len(UTTERANCES)
    assert [c[0] for c in fakes.tts.calls][EDITED_INDEX] == EDITED
    assert {c[1] for c in fakes.tts.calls} == {VOICE_ID}

    # artifacts
    for key in ("audio", "voice_track", "mixed_audio", "srt", "output", "srt_output"):
        assert Path(job.artifacts[key]).is_file(), key
    assert all(seg.aligned_path and Path(seg.aligned_path).is_file() for seg in job.segments)

    # output video: video stream copied, audio as long as the video
    output = Path(job.artifacts["output"])
    assert output.name == "视频.vi.mp4"
    assert output.parent == config.paths.output
    src, out = probe_streams(video), probe_streams(output)
    assert out["video"]["codec_name"] == src["video"]["codec_name"] == "h264"
    assert out["audio"]["codec_name"] == "aac"
    assert abs(out["audio"]["duration"] - out["video"]["duration"]) < 0.15
    assert abs(out["video"]["duration"] - src["video"]["duration"]) < 0.15

    # subtitles: edited line present, timestamps from the transcription
    srt_file = Path(job.artifacts["srt_output"])
    assert srt_file.name == "视频.vi.srt"
    blocks = srt_blocks(srt_file)
    assert len(blocks) == len(UTTERANCES)
    start, end, _zh = UTTERANCES[EDITED_INDEX]
    assert blocks[EDITED_INDEX] == (f"{srt_time(start)} --> {srt_time(end)}", EDITED)
    assert blocks[0][1] == vietnamese_for(UTTERANCES[0][2])

    # ducking: the music (220 Hz) is clearly quieter while the voice (1 kHz) speaks
    mixed = Path(job.artifacts["mixed_audio"])
    # utterance 1 ends at about 7.4 s; the (edited, longer) utterance 2 speaks from 8.5 s to 11.3 s
    speech_window, gap_window = (9.0, 10.5), (7.8, 8.3)
    bed = BED_FREQS[0]
    in_speech = tone_amplitude(mixed, bed, *speech_window)
    in_gap = tone_amplitude(mixed, bed, *gap_window)
    assert in_gap > 0
    assert in_speech < 0.6 * in_gap, (in_speech, in_gap)
    assert tone_amplitude(mixed, VOICE_FREQ, *speech_window) > 10 * tone_amplitude(
        mixed, VOICE_FREQ, *gap_window
    )
    # the voice track itself is silent in the gaps and full length
    voice = Path(job.artifacts["voice_track"])
    assert tone_amplitude(voice, VOICE_FREQ, *gap_window) < 1e-3
    assert tone_amplitude(voice, VOICE_FREQ, *speech_window) > 0.01


@needs_ffmpeg
def test_gemini_quota_pause_then_resume_does_not_retranslate(tmp_path, monkeypatch):
    fakes = install_fakes(monkeypatch, tmp_path)
    fakes.gemini.translate_quota_after = 2  # the third batch hits the daily quota
    config = make_config(tmp_path, review=False)
    _video, job = new_job(tmp_path, config)

    job = pipeline_for(config).run(job)
    assert job.status == JobStatus.PAUSED
    assert "quota" in job.message.lower()
    assert "resume" in job.message
    assert job.current_stage == "translate"
    assert "translate" not in job.completed_stages
    assert len(fakes.gemini.asr_calls) == 3
    assert len(fakes.gemini.translate_prompts) == 2
    on_disk = Job.load(job.work_dir)
    assert on_disk.status == JobStatus.PAUSED
    assert sum(1 for s in on_disk.segments if s.text_vi) == 6  # two finished batches kept
    assert fakes.tts.calls == []

    fakes.gemini.translate_quota_after = None  # "tomorrow": the quota is back
    job = pipeline_for(config).resume(Job.load(job.work_dir))
    assert job.status == JobStatus.DONE, job.error
    assert len(fakes.gemini.asr_calls) == 3  # transcription not repeated
    assert len(fakes.gemini.translate_prompts) == 3  # only the missing batch was translated
    assert all(s.text_vi == vietnamese_for(s.text_zh) for s in job.segments)
    assert len(fakes.tts.calls) == len(UTTERANCES)
    assert Path(job.artifacts["output"]).is_file()


@needs_ffmpeg
def test_elevenlabs_budget_too_small_pauses_before_synthesizing(tmp_path, monkeypatch):
    fakes = install_fakes(monkeypatch, tmp_path)
    config = make_config(tmp_path, review=False)
    config.elevenlabs.monthly_character_budget = 50  # the video needs a few hundred
    _video, job = new_job(tmp_path, config)

    job = pipeline_for(config).run(job)
    assert job.status == JobStatus.PAUSED
    assert job.current_stage == "synthesize"
    assert "Not enough ElevenLabs characters" in job.message
    assert "Nothing was synthesized" in job.message
    assert fakes.tts.calls == []
    assert not list((job.work_dir / "tts").glob("*"))
    assert all(seg.audio_path is None for seg in job.segments)
    assert "synthesize" not in job.completed_stages
    assert "subtitles" in job.completed_stages  # free work before it is kept

    # raise the budget: the same job resumes and finishes, paying exactly once per line
    config.elevenlabs.monthly_character_budget = 10000
    job = pipeline_for(config).resume(Job.load(job.work_dir))
    assert job.status == JobStatus.DONE, job.error
    assert len(fakes.tts.calls) == len(UTTERANCES)
    assert len(fakes.gemini.asr_calls) == 3
