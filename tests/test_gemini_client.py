from __future__ import annotations

from pathlib import Path

import pytest

from video_translator.providers.gemini_client import (
    FakeGeminiTransport,
    GeminiAPIError,
    GeminiClient,
    GeminiError,
    QuotaExhaustedError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make(responses, rpm=2, **kw):
    clock = FakeClock()
    transport = FakeGeminiTransport(responses)
    client = GeminiClient(
        model="m", requests_per_minute=rpm, transport=transport,
        clock=clock, sleep=clock.sleep, **kw,
    )  # fmt: skip
    return client, transport, clock


def test_returns_text_and_records_call():
    client, transport, _ = make(["hello"])
    audio = Path("a.wav")
    assert client.generate_text("p", audio_path=audio, json_mode=True) == "hello"
    assert transport.prompts == ["p"]
    assert transport.calls[0] == {"prompt": "p", "audio_path": audio, "json_mode": True}


def test_rate_limiter_sleeps_when_window_full():
    client, _, clock = make(["1", "2", "3"], rpm=2)
    client.generate_text("a")
    client.generate_text("b")
    assert clock.sleeps == []
    client.generate_text("c")  # third call within 60 s must wait
    assert clock.sleeps == [pytest.approx(60.0)]


def test_no_sleep_when_calls_are_spread_out():
    client, _, clock = make(["1", "2", "3"], rpm=1)
    for prompt in "abc":
        client.generate_text(prompt)
        clock.now += 61
    assert clock.sleeps == []


def test_retries_transient_errors_with_backoff():
    errors = [GeminiAPIError(429, "rate limit per minute"), GeminiAPIError(503, "unavailable")]
    client, transport, clock = make([*errors, "ok"], rpm=100, base_delay=2.0)
    assert client.generate_text("p") == "ok"
    assert len(transport.prompts) == 3
    assert clock.sleeps == [2.0, 4.0]


def test_gives_up_after_max_retries():
    client, transport, _ = make([GeminiAPIError(500, "x")] * 3, rpm=100, max_retries=2)
    with pytest.raises(GeminiError, match="3 attempts"):
        client.generate_text("p")
    assert len(transport.prompts) == 3


def test_daily_quota_raises_without_retry():
    err = GeminiAPIError(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier exceeded")
    client, transport, clock = make([err, "never"], rpm=100)
    with pytest.raises(QuotaExhaustedError):
        client.generate_text("p")
    assert len(transport.prompts) == 1 and clock.sleeps == []


def test_non_transient_error_is_not_retried():
    client, transport, _ = make([GeminiAPIError(400, "bad request")], rpm=100)
    with pytest.raises(GeminiError):
        client.generate_text("p")
    assert len(transport.prompts) == 1


def test_unknown_exception_wrapped():
    client, _, _ = make([ValueError("weird")], rpm=100)
    with pytest.raises(GeminiError, match="ValueError"):
        client.generate_text("p")


def test_fake_transport_runs_out_of_responses():
    transport = FakeGeminiTransport()
    with pytest.raises(GeminiError):
        transport.generate_text("p")


def test_missing_api_key_gives_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = GeminiClient(model="m", requests_per_minute=10)
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        client.generate_text("p")
