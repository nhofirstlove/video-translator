from __future__ import annotations

import pytest

from video_translator.config import AppConfig, ConfigError, load_config


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("VT_CONFIG", raising=False)


def test_default_load():
    cfg = load_config()
    assert cfg.providers.asr == "whisper" and cfg.providers.tts == "vieneu"
    assert cfg.providers.translation == "ninerouter"
    assert cfg.ninerouter.base_url == "http://localhost:20128/v1" and cfg.ninerouter.models == [
        "free-combo"
    ]
    assert cfg.gemini.model == "gemini-3.6-flash"
    assert cfg.vieneu.mode == "library" and cfg.vieneu.voice == "Trúc Ly"
    assert cfg.chunk_minutes == 5 and cfg.review is True
    assert cfg.audio.ducked_volume == 0.25
    assert cfg == AppConfig()  # YAML and built-in defaults agree


def test_local_override_is_deep_merged(tmp_path):
    (tmp_path / "default.yaml").write_text(
        "gemini: {model: a, requests_per_minute: 10}\nreview: true\n", encoding="utf-8"
    )
    (tmp_path / "local.yaml").write_text("gemini: {model: b}\nreview: false\n", encoding="utf-8")
    cfg = load_config(tmp_path / "default.yaml")
    assert cfg.gemini.model == "b"
    assert cfg.gemini.requests_per_minute == 10
    assert cfg.review is False


def test_vt_config_env_var(tmp_path, monkeypatch):
    path = tmp_path / "x.yaml"
    path.write_text("chunk_minutes: 5\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(path))
    assert load_config().chunk_minutes == 5


def test_invalid_value_gives_clear_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("chunk_minutes: 0\ngemini: {requests_per_minute: -1}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="chunk_minutes"):
        load_config(path)


def test_unknown_key_and_missing_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("devise: cpu\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="devise"):
        load_config(bad)
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_python_dash_m_entry_point_runs():
    """`python -m video_translator doctor --help` must work even without the console script."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "video_translator", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "doctor" in result.stdout
