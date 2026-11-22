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
    assert "gemini" in registry.available("asr")
    assert "gemini" in registry.available("translation")
    assert "elevenlabs" in registry.available("tts")


@pytest.mark.parametrize(
    "module",
    [
        "video_translator.providers",
        "video_translator.providers.asr",
        "video_translator.providers.translation",
        "video_translator.providers.tts",
        "video_translator.stages.transcribe",
        "video_translator.stages.translate",
        "video_translator.stages.synthesize",
    ],
)
def test_importing_any_entry_point_registers_v1_providers(module):
    """Each entry point works on its own, in a fresh interpreter (no import-order luck)."""
    import subprocess
    import sys

    code = (
        f"import {module}\n"
        "from video_translator.providers import registry\n"
        "assert 'gemini' in registry.available('asr')\n"
        "assert 'gemini' in registry.available('translation')\n"
        "assert 'elevenlabs' in registry.available('tts')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
