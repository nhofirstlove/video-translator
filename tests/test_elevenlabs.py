import pytest

from video_translator.config import AppConfig
from video_translator.providers import registry
from video_translator.providers.tts import elevenlabs as el
from video_translator.providers.tts.elevenlabs import (
    ElevenLabsTTS,
    TTSAPIError,
    TTSConfigError,
    TTSError,
    TTSQuotaExceededError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def convert(self, text, voice_id, model_id):
        self.calls.append((text, voice_id, model_id))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def list_voices(self):
        return [{"voice_id": "v1", "name": "Mai", "category": "shared"}]


def make(responses, voice="voice123"):
    cfg = AppConfig()
    cfg.elevenlabs.voice = voice
    transport = FakeTransport(responses)
    sleeps = []
    tts = ElevenLabsTTS(config=cfg, transport=transport, sleep=sleeps.append)
    return tts, transport, sleeps


def test_registered():
    assert registry.get("tts", "elevenlabs") is ElevenLabsTTS


def test_writes_audio_and_passes_voice_and_model(tmp_path):
    tts, transport, _ = make([b"AUDIO"])
    out = tts.synthesize("xin chào", tmp_path / "sub" / "a.mp3")
    assert out.read_bytes() == b"AUDIO"
    assert transport.calls == [("xin chào", "voice123", "eleven_multilingual_v2")]


def test_voice_argument_overrides_config(tmp_path):
    tts, transport, _ = make([b"A"])
    tts.synthesize("x", tmp_path / "a.mp3", voice="other")
    assert transport.calls[0][1] == "other"


def test_missing_voice_gives_clear_error(tmp_path):
    tts, transport, _ = make([b"A"], voice="")
    with pytest.raises(TTSConfigError, match=r"elevenlabs\.voice"):
        tts.synthesize("x", tmp_path / "a.mp3")
    assert transport.calls == []


def test_retries_429_and_5xx_with_backoff(tmp_path):
    tts, transport, sleeps = make([TTSAPIError(429, "slow down"), TTSAPIError(503, "x"), b"OK"])
    tts.synthesize("x", tmp_path / "a.mp3")
    assert len(transport.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_attempts(tmp_path):
    tts, transport, _ = make([TTSAPIError(500, "boom")] * el.MAX_ATTEMPTS)
    with pytest.raises(TTSError):
        tts.synthesize("x", tmp_path / "a.mp3")
    assert len(transport.calls) == el.MAX_ATTEMPTS


def test_client_error_is_not_retried(tmp_path):
    tts, transport, _ = make([TTSAPIError(400, "bad"), b"OK"])
    with pytest.raises(TTSError):
        tts.synthesize("x", tmp_path / "a.mp3")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        TTSAPIError(401, '{"detail":{"status":"quota_exceeded","message":"x"}}'),
        TTSAPIError(429, "You have insufficient credits"),
    ],
)
def test_quota_error_is_mapped_and_not_retried(tmp_path, error):
    tts, transport, _ = make([error, b"OK"])
    with pytest.raises(TTSQuotaExceededError):
        tts.synthesize("x", tmp_path / "a.mp3")
    assert len(transport.calls) == 1
    assert not (tmp_path / "a.mp3").exists()


def test_missing_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv(el.API_KEY_ENV, raising=False)
    cfg = AppConfig()
    cfg.elevenlabs.voice = "v"
    with pytest.raises(TTSConfigError, match="ELEVENLABS_API_KEY"):
        ElevenLabsTTS(config=cfg).synthesize("x", tmp_path / "a.mp3")


def test_list_voices_best_effort(monkeypatch):
    tts, _, _ = make([])
    assert tts.list_voices()[0]["voice_id"] == "v1"
    monkeypatch.delenv(el.API_KEY_ENV, raising=False)
    assert ElevenLabsTTS(config=AppConfig()).list_voices() == []
