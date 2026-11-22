from __future__ import annotations

import json

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.providers.gemini_client import (
    FakeGeminiTransport,
    GeminiClient,
    QuotaExhaustedError,
)
from video_translator.providers.translation.gemini import (
    GeminiTranslation,
    TranslationError,
    max_chars_for,
    parse_response,
)
from video_translator.stages.base import PauseRequested
from video_translator.stages.translate import TranslateStage


def make_client(*responses):
    fake = FakeGeminiTransport(list(responses))
    client = GeminiClient(model="m", requests_per_minute=1000, transport=fake, sleep=lambda s: None)
    return client, fake


def segs(n, vi=""):
    return [
        Segment(start=i * 2.0, end=i * 2.0 + 2.0, text_zh=f"zh{i}", text_vi=vi) for i in range(n)
    ]


def answer(ids, prefix="vi"):
    return json.dumps({"segments": [{"id": i, "vi": f"{prefix}{i}"} for i in ids]})


def test_max_chars():
    assert max_chars_for(2.4) == 36
    assert max_chars_for(0.1) >= 8


def test_batch_splitting_and_order():
    client, fake = make_client(answer(range(2), "a"), answer(range(2), "b"), answer(range(1), "c"))
    provider = GeminiTranslation(client=client, batch_size=2)
    original = segs(5)
    out = provider.translate(original)
    assert [s.text_vi for s in out] == ["a0", "a1", "b0", "b1", "c0"]
    assert all(s.text_vi == "" for s in original)  # input not mutated
    assert len(fake.prompts) == 3
    assert all(c["json_mode"] for c in fake.calls)


def test_context_passed_and_prompt_contents():
    client, fake = make_client(answer(range(2), "a"), answer(range(2), "b"))
    provider = GeminiTranslation(client=client, batch_size=2, context_size=3)
    provider.translate(segs(4))
    assert '"context": [' not in fake.prompts[0]
    assert '"seconds": 2.0' in fake.prompts[0] and '"max_chars": 30' in fake.prompts[0]
    second = fake.prompts[1]
    assert '"context": [' in second
    assert '"vi": "a0"' in second and '"vi": "a1"' in second
    assert '"zh": "zh2"' in second


def test_fenced_json_and_missing_id_retry():
    client, fake = make_client(
        "```json\n" + answer([0]) + "\n```",  # id 1 missing -> retry
        "```\n" + answer([0, 1], "ok") + "\n```",
    )
    out = GeminiTranslation(client=client).translate(segs(2))
    assert [s.text_vi for s in out] == ["ok0", "ok1"]
    assert len(fake.prompts) == 2


def test_unusable_twice_raises():
    client, fake = make_client("not json", answer([0]))
    with pytest.raises(TranslationError, match="2 attempts"):
        GeminiTranslation(client=client).translate(segs(2))
    assert len(fake.prompts) == 2


def test_parse_response_variants():
    assert parse_response('[{"id": 1, "vi": " x "}]', [1]) == {1: "x"}
    with pytest.raises(TranslationError):
        parse_response('{"foo": 1}', [0])
    with pytest.raises(TranslationError):
        parse_response('{"segments": [{"id": 0, "vi": "  "}]}', [0])


def make_job(tmp_path, n):
    return Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path, segments=segs(n))


def stage_with(client, batch_size=2):
    return TranslateStage(lambda cfg: GeminiTranslation(client=client, batch_size=batch_size))


def test_stage_translates_and_saves_batches(tmp_path):
    client, fake = make_client(answer(range(2), "a"), answer(range(1), "b"))
    job = make_job(tmp_path, 3)
    stage_with(client).run(job, AppConfig())
    assert [s.text_vi for s in job.segments] == ["a0", "a1", "b0"]
    assert job.message == "Translating 3/3"
    assert (tmp_path / "translations" / "batch_000.json").exists()
    assert (tmp_path / "translations" / "batch_001.json").exists()


def test_stage_quota_pause_then_resume_skips_finished(tmp_path):
    client, fake = make_client(answer(range(2), "a"), QuotaExhaustedError("daily"))
    job = make_job(tmp_path, 4)
    with pytest.raises(PauseRequested) as info:
        stage_with(client).run(job, AppConfig())
    assert info.value.status == JobStatus.PAUSED
    assert f"video-translator resume {tmp_path}" in info.value.message
    assert [s.text_vi for s in job.segments] == ["a0", "a1", "", ""]
    assert Job.load(tmp_path).segments[1].text_vi == "a1"

    # resume with a fresh job state (as if the process died before saving): batch files reused
    fresh = make_job(tmp_path, 4)
    client2, fake2 = make_client(answer(range(2), "b"))
    stage_with(client2).run(fresh, AppConfig())
    assert [s.text_vi for s in fresh.segments] == ["a0", "a1", "b0", "b1"]
    assert len(fake2.prompts) == 1

    # and a resume of the saved job translates only the remaining segments
    client3, fake3 = make_client()
    stage_with(client3).run(fresh, AppConfig())
    assert fake3.prompts == []


def test_stage_resume_from_saved_job_only_remaining(tmp_path):
    job = make_job(tmp_path, 4)
    job.segments[0].text_vi = "kept0"
    job.segments[1].text_vi = "kept1"
    client, fake = make_client(answer(range(2), "n"))
    stage_with(client).run(job, AppConfig())
    assert [s.text_vi for s in job.segments] == ["kept0", "kept1", "n0", "n1"]
    assert len(fake.prompts) == 1
    assert '"zh": "zh0"' not in fake.prompts[0].split('"context"')[0]


def test_stage_empty_segments_error(tmp_path):
    job = make_job(tmp_path, 0)
    with pytest.raises(ValueError, match="Nothing to translate"):
        stage_with(make_client()[0]).run(job, AppConfig())


def test_stage_uses_registry_default_factory(tmp_path, monkeypatch):
    client, _ = make_client(answer(range(1), "x"))
    monkeypatch.setattr(
        "video_translator.providers.translation.gemini.GeminiTranslation.client",
        property(lambda self: client),
    )
    job = make_job(tmp_path, 1)
    TranslateStage().run(job, AppConfig())
    assert job.segments[0].text_vi == "x0"
