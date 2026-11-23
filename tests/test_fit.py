"""Fitting the Vietnamese to each line's time: write to fit, shorten what is too long."""

from __future__ import annotations

import json

import pytest

from tests.test_ninerouter import FakeRouter, provider
from video_translator.config import AppConfig, FitConfig
from video_translator.models import Job, Segment
from video_translator.providers.gemini_client import (
    FakeGeminiTransport,
    GeminiClient,
    QuotaExhaustedError,
)
from video_translator.providers.translation.gemini import GeminiTranslation, TranslationError
from video_translator.providers.translation.ninerouter import NineRouterError
from video_translator.stages.translate import FIT_MARKER, TranslateStage


def gemini(*responses):
    fake = FakeGeminiTransport(list(responses))
    client = GeminiClient(model="m", requests_per_minute=1000, transport=fake, sleep=lambda s: None)
    return GeminiTranslation(client=client, config=AppConfig()), fake


def answer(*texts):
    return json.dumps({"segments": [{"id": i, "vi": t} for i, t in enumerate(texts)]})


def payload(prompt: str) -> dict:
    return json.loads(prompt.split("Input JSON:\n")[1].split("\n\nReply")[0])


LONG = "một hai ba bốn năm sáu bảy tám chín mười mười một mười hai mười ba"  # 63 chars


def test_prompt_budget_uses_the_configured_speaking_rate_and_allows_rewording():
    cfg = AppConfig()
    cfg.fit = FitConfig(chars_per_second=10.0)
    g = GeminiTranslation(client=None, config=cfg)
    seg = Segment(start=0.0, end=2.0, text_zh="你好")
    prompt = g._build_prompt([seg], [])
    assert payload(prompt)["segments"][0]["max_chars"] == 20
    assert "about 10 characters per second" in prompt
    assert "shorten and rephrase freely" in prompt


def test_gemini_shorten_batch_sends_current_text_and_limits():
    g, fake = gemini(answer("ngắn hơn"))
    seg = Segment(start=0.0, end=2.0, text_zh="你好", text_vi=LONG)
    assert g.shorten_batch([seg], [20]) == ["ngắn hơn"]
    sent = payload(fake.calls[0]["prompt"])["segments"][0]
    assert sent["vi"] == LONG and sent["max_chars"] == 20 and sent["zh"] == "你好"
    bad, _ = gemini("not json", "still not")
    with pytest.raises(TranslationError):
        bad.shorten_batch([seg], [20])


def test_ninerouter_shortens_with_free_models_first_then_gemini():
    seg = Segment(start=0.0, end=2.0, text_zh="你好", text_vi=LONG)
    router = FakeRouter({"a": [answer("rút gọn")]})
    p = provider(router, models=("a",))
    assert p.shorten_batch([seg], [20]) == ["rút gọn"]
    assert p.lines_by == {}  # shortening is not counted as translating
    down = FakeRouter({"a": [NineRouterError("down")]})
    p2 = provider(down, models=("a",), gemini=FakeGeminiTransport([answer("do Gemini")]))
    assert p2.shorten_batch([seg], [20]) == ["do Gemini"]
    p3 = provider(
        FakeRouter({"a": [NineRouterError("down")]}),
        models=("a",),
        gemini=FakeGeminiTransport([ValueError("no key")]),
    )
    with pytest.raises(TranslationError, match="could shorten this batch"):
        p3.shorten_batch([seg], [20])


class Shortener:
    """A translation provider stand-in that already translated and can shorten."""

    batch_size = 40
    context_size = 3

    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[list[int]] = []
        self.notes: list[str] = []

    def translate_batch(self, batch, context=()):
        return [f"vi {s.text_zh}" for s in batch]

    def shorten_batch(self, batch, limits):
        self.calls.append(list(limits))
        if self.error:
            raise self.error
        return self.replies.pop(0)


def job_with(tmp_path, texts, seconds=1.0):
    job = Job.create(tmp_path / "v.mp4", tmp_path / "work")
    job.segments = [
        Segment(start=i * 3.0, end=i * 3.0 + seconds, text_zh=f"zh{i}", text_vi=t)
        for i, t in enumerate(texts)
    ]
    return job


def run(job, prov):
    return TranslateStage(lambda cfg: prov).run(job, AppConfig())


def test_long_lines_are_rewritten_and_short_ones_left_alone(tmp_path):
    # 1 s slot: budget 15 chars, "too long" above 18
    job = job_with(tmp_path, ["ngắn", LONG, "vừa phải đây"])
    prov = Shortener(replies=[["rút gọn nè"]])
    run(job, prov)
    assert [s.text_vi for s in job.segments] == ["ngắn", "rút gọn nè", "vừa phải đây"]
    assert prov.calls == [[16]]  # round 1 asks for 1.1 x the budget
    assert "1 long line(s) were shortened" in job.message
    assert (job.work_dir / "translations" / FIT_MARKER).exists()
    run(job, prov)  # the check is not repeated on a resume
    assert len(prov.calls) == 1


def test_second_round_is_stricter_and_a_longer_answer_is_ignored(tmp_path):
    job = job_with(tmp_path, [LONG])
    still_long = "một hai ba bốn năm sáu bảy tám chín mười mười một"  # 49 chars
    prov = Shortener(replies=[[still_long], [LONG + " nữa nhé"], ["x"]])
    run(job, prov)
    assert prov.calls == [[16], [15]]  # two rounds only (fit.rewrite_rounds = 2)
    assert job.segments[0].text_vi == still_long  # round 2 answered with a longer line: kept
    assert "still long" in job.message


def test_a_failed_shortening_keeps_the_text_and_is_retried_on_resume(tmp_path):
    job = job_with(tmp_path, [LONG])
    prov = Shortener(error=TranslationError("bad answer"))
    run(job, prov)
    assert job.segments[0].text_vi == LONG
    assert "shortening stopped" in job.message
    assert not (job.work_dir / "translations" / FIT_MARKER).exists()
    quota = Shortener(error=QuotaExhaustedError("day"))
    run(job, quota)  # a quota stop does not fail or pause the job
    assert "quota" in job.message


def test_rewrite_rounds_zero_turns_the_check_off_and_providers_without_it_are_fine(tmp_path):
    job = job_with(tmp_path, [LONG])
    prov = Shortener(replies=[["x"]])
    cfg = AppConfig()
    cfg.fit.rewrite_rounds = 0
    TranslateStage(lambda c: prov).run(job, cfg)
    assert prov.calls == []

    class Plain:
        batch_size = 40
        context_size = 3

        def translate_batch(self, batch, context=()):
            return ["x"] * len(batch)

    job2 = job_with(tmp_path / "b", [LONG])
    TranslateStage(lambda c: Plain()).run(job2, AppConfig())
    assert job2.segments[0].text_vi == LONG


def test_fit_defaults():
    cfg = AppConfig().fit
    assert cfg.chars_per_second == 15.0 and cfg.rewrite_rounds == 2
