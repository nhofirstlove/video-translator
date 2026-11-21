from __future__ import annotations

import pytest

from video_translator.providers import registry


def test_register_and_get():
    @registry.register("dummy_kind", "one")
    class One:
        pass

    assert registry.get("dummy_kind", "one") is One
    assert "one" in registry.available("dummy_kind")


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        registry.get("dummy_kind", "missing")


def test_builtin_providers_registered():
    import video_translator.providers  # noqa: F401

    assert "demucs" in registry.available("separation")
    assert "faster_whisper" in registry.available("asr")
    assert "llm" in registry.available("translation")
    assert "placeholder" in registry.available("tts")
