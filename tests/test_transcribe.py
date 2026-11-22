from __future__ import annotations

import json

import pytest

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job, JobStatus
from video_translator.providers.asr.gemini import GeminiASR, parse_segments
from video_translator.providers.gemini_client import (
    FakeGeminiTransport,
    GeminiAPIError,
    GeminiClient,
    GeminiError,
    QuotaExhaustedError,
)
from video_translator.stages.base import PauseRequested
from video_translator.stages.transcribe import TranscribeStage


def make_asr(responses):
    fake = FakeGeminiTransport(responses)
    client = GeminiClient(model="m", requests_per_minute=1000, transport=fake, sleep=lambda s: None)
    return GeminiASR(client=client), fake


def reply(*items):
    return json.dumps(
        {"segments": [{"start": s, "end": e, "text": t} for s, e, t in items]}, ensure_ascii=False
    )


# --- parsing -----------------------------------------------------------------------------


def test_parse_code_fence_and_mmss():
    raw = '```json\n{"segments":[{"start":"00:05","end":"00:08.5","text":"你好"}]}\n```'
    (seg,) = parse_segments(raw)
    assert (seg.start, seg.end, seg.text_zh) == (5.0, 8.5, "你好")


def test_parse_sorts_drops_empty_and_clamps():
    raw = reply((10, 12, "第二"), (1, 1, "第一"), (3, 4, "  "), (50, 60, "太晚"), (11, 30, "第三"))
    segs = parse_segments(raw, duration=20)
    assert [s.text_zh for s in segs] == ["第一", "第二", "第三"]
    assert segs[0].end > segs[0].start
    assert segs[1].end == 11  # overlap with the next segment fixed
    assert segs[2].end == 20  # clamped to the chunk duration


def test_parse_overlap_same_start_keeps_both():
    segs = parse_segments(reply((1, 4, "甲"), (1, 3, "乙")))
    assert len(segs) == 2 and segs[0].end <= segs[1].start and segs[1].end > segs[1].start


def test_parse_empty_variants():
    assert parse_segments('{"segments": []}') == []
    assert parse_segments("[]") == []


def test_parse_bare_list_and_truncated():
    assert len(parse_segments('[{"start": 1, "end": 2, "text": "好"}]')) == 1
    cut = '{"segments":[{"start":1,"end":2,"text":"好"},{"start":3,"end":4,"te'
    assert [s.text_zh for s in parse_segments(cut)] == ["好"]


def test_parse_garbage_raises():
    with pytest.raises(GeminiError):
        parse_segments("sorry, I cannot do that")


def test_provider_prompt_and_retry_on_bad_json(tmp_path):
    asr, fake = make_asr(["not json", reply((0, 2, "你好"))])
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    segs = asr.transcribe(audio)
    assert [s.text_zh for s in segs] == ["你好"] and len(fake.calls) == 2
    call = fake.calls[0]
    assert call["json_mode"] is True and call["audio_path"] == audio
    assert "Simplified Chinese" in call["prompt"] and "Do NOT translate" in call["prompt"]


# --- stage -------------------------------------------------------------------------------


def make_job(tmp_path, n=3, length=20.0, overlap=5.0):
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "work")
    step = length - overlap
    for i in range(n):
        start = i * step
        job.chunks.append(
            Chunk(index=i, start=start, end=start + length, path=tmp_path / f"c{i}.wav")
        )
    return job


def three_chunk_replies():
    # chunks: [0,20] [15,35] [30,50]; shared speech at 16-19 and 31-34 (absolute)
    return [
        reply((1, 4, "开头"), (16, 19.5, "重叠一")),
        reply((2.5, 5, "重叠一"), (8, 12, "中间"), (16.2, 19, "重叠二")),
        reply((1.3, 4.2, "重叠二的一部分"), (10, 15, "结尾")),
    ]


def test_merge_three_overlapping_chunks(tmp_path):
    job = make_job(tmp_path)
    asr, fake = make_asr(three_chunk_replies())
    out = TranscribeStage(asr).run(job, AppConfig())
    assert [s.text_zh for s in out.segments] == ["开头", "重叠一", "中间", "重叠二", "结尾"]
    starts = [s.start for s in out.segments]
    assert starts == sorted(starts)
    assert all(
        a.end <= b.start + 1e-9 for a, b in zip(out.segments, out.segments[1:], strict=False)
    )
    # chunk-relative times were shifted by chunk.start
    assert out.segments[0].start == 1 and out.segments[-1].start == 40
    # the copy of "重叠一" away from the chunk edge (chunk 1, t=17.5) wins over the edge one
    assert out.segments[1].start == pytest.approx(17.5)
    assert len(fake.calls) == 3
    assert out.message.startswith("Transcribed 5 segments")
    for i in range(3):
        assert (job.work_dir / "transcripts" / f"chunk_{i:03d}.json").is_file()


def test_resume_reuses_cache(tmp_path):
    job = make_job(tmp_path)
    asr, fake = make_asr(three_chunk_replies()[:2] + [QuotaExhaustedError("daily quota")])
    with pytest.raises(PauseRequested) as info:
        TranscribeStage(asr).run(job, AppConfig())
    assert info.value.status == JobStatus.PAUSED
    assert "resume" in info.value.message and "quota" in info.value.message
    assert (job.work_dir / "transcripts" / "chunk_000.json").is_file()
    assert (job.work_dir / "transcripts" / "chunk_001.json").is_file()
    assert not (job.work_dir / "transcripts" / "chunk_002.json").exists()
    assert len(fake.calls) == 3

    asr2, fake2 = make_asr([three_chunk_replies()[2]])
    out = TranscribeStage(asr2).run(job, AppConfig())
    assert len(fake2.calls) == 1  # only the unfinished chunk
    assert [s.text_zh for s in out.segments] == ["开头", "重叠一", "中间", "重叠二", "结尾"]

    asr3, fake3 = make_asr([])
    TranscribeStage(asr3).run(job, AppConfig())
    assert fake3.calls == []


def test_quota_via_api_error_becomes_pause(tmp_path):
    job = make_job(tmp_path, n=1)
    err = GeminiAPIError(429, "Quota exceeded: GenerateRequestsPerDay")
    asr, _ = make_asr([err])
    with pytest.raises(PauseRequested) as info:
        TranscribeStage(asr).run(job, AppConfig())
    assert info.value.status == JobStatus.PAUSED


def test_stale_cache_ignored(tmp_path):
    job = make_job(tmp_path, n=1)
    path = job.work_dir / "transcripts" / "chunk_000.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"index": 0, "start": 99, "end": 120, "segments": []}), encoding="utf-8"
    )
    asr, fake = make_asr([reply((1, 2, "好"))])
    out = TranscribeStage(asr).run(job, AppConfig())
    assert len(fake.calls) == 1 and len(out.segments) == 1


def test_no_chunks_is_an_error(tmp_path):
    job = Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path / "w")
    with pytest.raises(ValueError):
        TranscribeStage(make_asr([])[0]).run(job, AppConfig())


def test_provider_built_from_registry(tmp_path):
    from video_translator.providers import registry

    assert "gemini" in registry.available("asr") or TranscribeStage()  # registered on import
    assert registry.get("asr", "gemini") is GeminiASR
