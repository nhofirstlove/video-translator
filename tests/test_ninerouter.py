"""9Router translation (free models first, Gemini last) and the Whisper-only transcription."""

from __future__ import annotations

import json

import pytest

from video_translator.config import AppConfig, NineRouterConfig, WhisperConfig
from video_translator.models import Segment
from video_translator.providers.asr.cleanup import BLEEP_MARK, is_hallucination
from video_translator.providers.asr.whisper_only import WhisperOnlyASR
from video_translator.providers.asr.whisper_timing import TimedLine, Word
from video_translator.providers.gemini_client import (
    FakeGeminiTransport,
    GeminiClient,
    QuotaExhaustedError,
)
from video_translator.providers.translation.gemini import GeminiTranslation, TranslationError
from video_translator.providers.translation.ninerouter import (
    NineRouterError,
    NineRouterTranslation,
    check_vietnamese,
)

SEGS = [
    Segment(start=0.0, end=2.0, text_zh="你好吗"),
    Segment(start=2.5, end=4.0, text_zh="我很好"),
]


def reply(*pairs: str) -> str:
    return json.dumps({"segments": [{"id": i, "vi": t} for i, t in enumerate(pairs)]})


class FakeRouter:
    """9Router stand-in: answers per model from a script of replies / errors."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def chat(self, model: str, prompt: str) -> str:
        self.calls.append(model)
        item = self.script[model].pop(0) if self.script[model] else NineRouterError("empty", 500)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def provider(router, models=("free-combo",), gemini: FakeGeminiTransport | None = None, clock=None):
    clock = clock or Clock()
    fallback = GeminiTranslation(
        client=GeminiClient(transport=gemini or FakeGeminiTransport([]), sleep=lambda s: None)
    )
    return NineRouterTranslation(
        transport=router,
        fallback=fallback,
        settings=NineRouterConfig(),
        models=list(models),
        clock=clock,
        sleep=clock.sleep,
    )


def test_first_free_model_translates_and_gemini_is_not_used():
    router = FakeRouter({"free-combo": [reply("Chào bạn", "Tôi khỏe")]})
    gemini = FakeGeminiTransport([])
    p = provider(router, gemini=gemini)
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router.calls == ["free-combo"] and gemini.calls == []
    assert p.notes == ["translated: 2 lines by free-combo"]


def test_next_free_model_is_tried_before_gemini():
    router = FakeRouter(
        {
            "a": [NineRouterError("limit", 429)],
            "b": [reply("Chào bạn", "Tôi khỏe")],
        }
    )
    gemini = FakeGeminiTransport([])
    p = provider(router, models=("a", "b"), gemini=gemini)
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router.calls == ["a", "b"] and gemini.calls == []
    # model "a" is out of quota: the next batch does not ask it again
    router.script["b"].append(reply("Xin chào", "Mình ổn"))
    p.translate_batch(SEGS)
    assert router.calls == ["a", "b", "b"]
    assert p.lines_by == {"b": 4}


def test_unreadable_answer_is_asked_again_once_then_next_model():
    router = FakeRouter(
        {"a": ["not json at all", reply("Chào bạn", "Tôi khỏe")], "b": [reply("X", "Y")]}
    )
    p = provider(router, models=("a", "b"))
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router.calls == ["a", "a"]
    router2 = FakeRouter({"a": ["bad", "worse"], "b": [reply("Chào bạn", "Tôi khỏe")]})
    p2 = provider(router2, models=("a", "b"))
    assert p2.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router2.calls == ["a", "a", "b"]


def test_a_chinese_or_copied_answer_is_not_accepted():
    check_vietnamese(["你好"], ["Xin chào"])
    with pytest.raises(TranslationError):
        check_vietnamese(["你好吗"], ["你好吗，我很好"])
    with pytest.raises(TranslationError):
        check_vietnamese(["OK"], ["OK"])
    router = FakeRouter({"a": [reply("你好吗", "我很好"), reply("你好吗", "我很好")]})
    gemini = FakeGeminiTransport([reply("Chào bạn", "Tôi khỏe")])
    p = provider(router, models=("a",), gemini=gemini)
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]  # weak model: Gemini took over


def test_gemini_takes_over_only_after_every_free_model_failed():
    router = FakeRouter(
        {
            "a": [NineRouterError("down"), NineRouterError("down")],
            "b": [NineRouterError("quota", 402)],
        }
    )
    gemini = FakeGeminiTransport([reply("Chào bạn", "Tôi khỏe")])
    p = provider(router, models=("a", "b"), gemini=gemini)
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router.calls == ["a", "b"] and len(gemini.calls) == 1
    assert p.lines_by == {"Gemini": 2}
    assert any("Gemini took over 2 line(s)" in n for n in p.notes)
    # the free models are resting, so the next batch goes to Gemini straight away
    gemini.queue(reply("Xin chào", "Mình ổn"))
    p.translate_batch(SEGS)
    assert router.calls == ["a", "b"]


def test_resting_models_come_back_after_their_cooldown():
    clock = Clock()
    router = FakeRouter({"a": [NineRouterError("quota", 429), reply("Chào bạn", "Tôi khỏe")]})
    gemini = FakeGeminiTransport([reply("X", "Y")])
    p = provider(router, models=("a",), gemini=gemini, clock=clock)
    p.translate_batch(SEGS)  # a fails (429) -> Gemini
    clock.now += 1000  # more than the 15 minute rest
    assert p.translate_batch(SEGS) == ["Chào bạn", "Tôi khỏe"]
    assert router.calls == ["a", "a"]


def test_gemini_quota_pauses_and_other_gemini_errors_explain():
    router = FakeRouter({"a": [NineRouterError("down")]})
    p = provider(router, models=("a",), gemini=FakeGeminiTransport([QuotaExhaustedError("day")]))
    with pytest.raises(QuotaExhaustedError):
        p.translate_batch(SEGS)
    router = FakeRouter({"a": [NineRouterError("not reachable")]})
    p = provider(router, models=("a",), gemini=FakeGeminiTransport([ValueError("no key")]))
    with pytest.raises(TranslationError, match="No free model.*Gemini fallback failed"):
        p.translate_batch(SEGS)


def test_rate_limit_waits_between_requests():
    clock = Clock()
    router = FakeRouter({"a": [reply("Chào bạn", "Tôi khỏe")] * 3})
    p = provider(router, models=("a",), clock=clock)
    p.settings = NineRouterConfig(requests_per_minute=2)
    start = clock.now
    for _ in range(3):
        p.translate_batch(SEGS)
    assert clock.now - start >= 59  # the third request waited for a free slot


def test_translate_uses_batches_and_context_and_bleep_rule_is_in_the_prompt():
    seen: list[str] = []
    payloads: list[dict] = []

    class Recorder:
        def chat(self, model: str, prompt: str) -> str:
            seen.append(prompt)
            payload = json.loads(prompt.split("Input JSON:\n")[1].split("\n\nReply")[0])
            payloads.append(payload)
            return reply(*[f"Câu số {i['id']}" for i in payload["segments"]])

    segs = [Segment(start=i, end=i + 0.9, text_zh=f"句子{i}") for i in range(5)]
    p = provider(Recorder())
    p.batch_size = 2
    out = p.translate(segs)
    assert [s.text_vi for s in out] == ["Câu số 0", "Câu số 1"] * 2 + ["Câu số 0"]
    assert len(seen) == 3
    assert payloads[1].get("context") and not payloads[0].get("context")
    assert "（哔）" in seen[0]  # the rule for bleeped words


def test_config_defaults_and_provider_is_registered():
    from video_translator.providers import registry

    cfg = AppConfig()
    assert cfg.providers.translation == "ninerouter" and cfg.providers.asr == "whisper"
    assert registry.get("translation", "ninerouter") is NineRouterTranslation
    assert cfg.ninerouter.models == ["free-combo"] and cfg.ninerouter.batch_size == 10


def test_http_transport_speaks_openai_chat_format(monkeypatch):
    import io

    from video_translator.providers.translation import ninerouter as nr

    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(request, timeout):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        sent["auth"] = request.get_header("Authorization")
        body = {"choices": [{"message": {"content": reply("Chào bạn", "Tôi khỏe")}}]}
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(nr.urllib.request, "urlopen", fake_open)
    monkeypatch.delenv("NINEROUTER_API_KEY", raising=False)
    t = nr._HttpTransport("http://localhost:20128/v1/", nr.PLACEHOLDER_KEY, 5)
    assert json.loads(t.chat("free-combo", "hello")) == {
        "segments": [{"id": 0, "vi": "Chào bạn"}, {"id": 1, "vi": "Tôi khỏe"}]
    }
    assert sent["url"] == "http://localhost:20128/v1/chat/completions"
    assert sent["body"]["model"] == "free-combo"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["auth"] == "Bearer sk_9router"


def test_http_transport_turns_failures_into_errors(monkeypatch):
    import urllib.error

    from video_translator.providers.translation import ninerouter as nr

    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(nr.urllib.request, "urlopen", refuse)
    with pytest.raises(NineRouterError, match="not reachable") as info:
        nr._HttpTransport("http://x/v1", "k", 1).chat("m", "p")
    assert info.value.code is None


# ----- Whisper only ---------------------------------------------------------------------


class FakeWhisper:
    def __init__(self, lines):
        self.lines = lines

    def transcribe(self, audio_path, language):
        return [
            TimedLine(x.start, x.end, x.text, list(x.words), x.censored, x.force)
            for x in self.lines
        ]


def whisper_asr(lines, beeps=()):
    asr = WhisperOnlyASR(
        whisper=WhisperConfig(),
        whisper_transport=FakeWhisper(lines),
    )
    asr.timer._beep_finder = lambda path: list(beeps)
    return asr


def test_whisper_only_gives_the_whispers_words_and_drops_fillers_and_inventions(tmp_path):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    lines = [
        TimedLine(0.0, 2.0, "你好吗"),
        TimedLine(3.0, 3.5, "嗯"),
        TimedLine(4.0, 5.0, "请不吝点赞"),
        TimedLine(6.0, 8.0, "我很好，谢谢"),
    ]
    asr = whisper_asr(lines)
    segs = asr.transcribe(audio, duration=20.0)
    assert [(s.start, s.end, s.text_zh) for s in segs] == [
        (0.0, 2.0, "你好吗"),
        (6.0, 8.0, "我很好，谢谢"),
    ]
    assert asr.cache_tag == "whisper" and asr.notes == []


def test_a_bleeped_word_is_marked_for_the_translator(tmp_path):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    ws = [Word(0.0, 1.0, "你"), Word(1.0, 2.0, "真"), Word(5.2, 5.6, "他")]
    lines = [TimedLine(0.0, 2.0, "你真", ws[:2]), TimedLine(8.0, 9.0, "好的")]
    asr = whisper_asr(lines, beeps=[(5.0, 5.8)])
    segs = asr.transcribe(audio, duration=20.0)
    marked = [s for s in segs if s.text_zh == BLEEP_MARK]
    assert len(marked) == 1 and marked[0].start == pytest.approx(4.95, abs=0.01)
    assert any("marked" in n and "review" in n for n in asr.notes)


def test_hallucination_check_only_matches_the_bare_phrase():
    assert is_hallucination("请不吝点赞！")
    assert is_hallucination("字幕由Amara.org社区提供")
    assert not is_hallucination("他说请不吝点赞这件事我们今天不能做，因为太晚了")


def test_beep_line_keeps_the_words_whisper_caught_under_it():
    from video_translator.providers.asr.whisper_timing import apply_beeps

    ws = [Word(0.0, 1.0, "你"), Word(5.1, 5.7, "他妈"), Word(6.0, 7.0, "的")]
    out = apply_beeps([TimedLine(0.0, 7.0, "你他妈的", ws)], [(5.0, 5.8)])
    censored = [x for x in out if x.censored]
    assert len(censored) == 1 and "".join(w.text for w in censored[0].words) == "他妈"


def test_default_providers_are_registered_by_the_stages():
    from video_translator.providers import registry
    from video_translator.providers.asr.whisper_only import WhisperOnlyASR
    from video_translator.stages import transcribe, translate  # noqa: F401

    cfg = AppConfig()
    assert registry.get("asr", cfg.providers.asr) is WhisperOnlyASR
    assert registry.get("translation", cfg.providers.translation) is NineRouterTranslation


def _words(spec):
    from video_translator.providers.asr.whisper_timing import Word

    return [Word(a, b, t) for a, b, t in spec]


def test_sentences_are_split_at_sentence_ends_using_word_times():
    from video_translator.providers.asr.whisper_timing import TimedLine, split_at_sentences

    words = _words(
        [
            (0.0, 0.5, "老婆,"),
            (0.5, 1.4, "我今晚加班不回家了。"),
            (1.4, 2.0, "我有"),
            (2.0, 3.6, "一个特殊的能力。"),
            (3.6, 4.4, "老公,"),
            (4.4, 5.76, "你回来了。"),
        ]
    )
    line = TimedLine(0.0, 5.76, "".join(w.text for w in words), words)
    parts = split_at_sentences(line)
    assert [p.text for p in parts] == [
        "老婆,我今晚加班不回家了。",
        "我有一个特殊的能力。",
        "老公,你回来了。",
    ]
    assert [(p.start, p.end) for p in parts] == [(0.0, 1.4), (1.4, 3.6), (3.6, 5.76)]


def test_sentence_split_keeps_crumbs_and_censored_lines_together():
    from video_translator.providers.asr.whisper_timing import TimedLine, split_at_sentences

    words = _words([(0.0, 1.0, "好的。"), (1.0, 1.15, "嗯"), (1.15, 2.0, "走吧")])
    line = TimedLine(0.0, 2.0, "好的。嗯走吧", words)
    assert [p.text for p in split_at_sentences(line)] == ["好的。", "嗯走吧"]
    tail = _words([(0.0, 1.0, "好的"), (1.0, 1.1, "。")])
    assert len(split_at_sentences(TimedLine(0.0, 1.1, "好的。", tail))) == 1  # tiny end piece
    censored = TimedLine(0.0, 2.0, "", words, censored=True)
    assert split_at_sentences(censored) == [censored]
    assert split_at_sentences(TimedLine(0.0, 2.0, "没有词", []))[0].text == "没有词"


def test_whisper_defaults_split_more():
    cfg = AppConfig().whisper
    assert cfg.pause_seconds == 0.2 and cfg.split_sentences is True


def test_doctor_checks_key_and_model_names():
    import io
    import urllib.error

    from video_translator.doctor import FAIL, OK, WARN, check_ninerouter

    def lists(*ids):
        body = json.dumps({"data": [{"id": i} for i in ids]}).encode()
        return lambda req, timeout: io.BytesIO(body)

    assert check_ninerouter("gemini").status == OK
    assert check_ninerouter(models=("free-combo",), opener=lists("free-combo", "kr/x")).status == OK
    missing = check_ninerouter(models=("free-combo",), opener=lists("kr/claude-sonnet-4.5"))
    assert missing.status == WARN and "kr/claude-sonnet-4.5" in missing.message
    assert "Combos" in missing.message

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    refused = check_ninerouter(opener=refuse)
    assert refused.status == WARN and "NINEROUTER_API_KEY" in refused.message
    assert FAIL not in refused.status


def test_unknown_model_error_tells_how_to_fix_it():
    error = NineRouterError("answered 404: No active credentials for provider: openai", 404)
    router = FakeRouter({"free-combo": [error]})
    p = provider(router, gemini=FakeGeminiTransport([ValueError("no key")]))
    with pytest.raises(TranslationError, match="create a combo"):
        p.translate_batch(SEGS)


def test_edited_settings_are_picked_up_by_the_next_job(tmp_path):
    from video_translator.web.jobs import JobManager

    old, new = AppConfig(), AppConfig()
    new.ninerouter.models = ["my-combo"]
    manager = JobManager(old, config_loader=lambda: new)
    manager._refresh_config()
    assert manager.config.ninerouter.models == ["my-combo"]
    assert manager.config.paths == old.paths

    def broken() -> AppConfig:
        raise ValueError("bad yaml")

    manager.config_loader = broken
    manager._refresh_config()  # keeps the settings it has
    assert manager.config.ninerouter.models == ["my-combo"]
    assert JobManager(old).config_loader is None
