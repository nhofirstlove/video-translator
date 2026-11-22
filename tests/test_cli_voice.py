"""``video-translator voices`` and ``say`` with a fake VieNeu transport (no package, no network)."""

from __future__ import annotations

import sys

import pytest

from tests.e2e_support import FakeVieNeu
from video_translator import cli, synth_line
from video_translator.config import AppConfig
from video_translator.providers.tts import vieneu
from video_translator.providers.tts.vieneu import VieNeuTTS


@pytest.fixture
def fake(monkeypatch):
    fake = FakeVieNeu()
    config = AppConfig()
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    monkeypatch.setattr(
        synth_line,
        "default_tts_factory",
        lambda cfg: VieNeuTTS(config=cfg, transport=fake),
    )
    fake.config = config
    return fake


def test_voices_lists_presets(fake, capsys):
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert "Minh Quân" in out and "Mai Anh" in out


def test_voices_in_server_mode_explains(fake, capsys):
    fake.config.vieneu.mode = "server"
    assert cli.main(["voices"]) == 0
    assert "Server mode" in capsys.readouterr().out


def test_voices_without_package_is_a_clear_message(monkeypatch, capsys):
    vieneu.reset_shared_model()
    monkeypatch.setitem(sys.modules, "vieneu", None)
    monkeypatch.setattr(cli, "load_config", lambda path=None: AppConfig())
    assert cli.main(["voices"]) == 1
    assert "pip install vieneu" in capsys.readouterr().err


def test_say_writes_default_file_and_prints_path(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["say", "Xin chào"]) == 0
    assert (tmp_path / "say.wav").is_file()
    assert "say.wav" in capsys.readouterr().out
    assert fake.calls == [("Xin chào", "Trúc Ly")]


def test_say_with_voice_and_out(fake, tmp_path, capsys):
    out = tmp_path / "x" / "sample.wav"
    assert cli.main(["say", "Chào", "--voice", "Mai Anh", "--out", str(out)]) == 0
    assert out.is_file() and str(out) in capsys.readouterr().out
    assert fake.calls == [("Chào", "Mai Anh")]


def test_say_unknown_voice_and_empty_text_fail_cleanly(fake, tmp_path, capsys):
    assert cli.main(["say", "Chào", "--voice", "Nobody", "--out", str(tmp_path / "a.wav")]) == 1
    assert "Some valid voices" in capsys.readouterr().err
    assert cli.main(["say", "  ", "--out", str(tmp_path / "b.wav")]) == 1
    assert "no text" in capsys.readouterr().err
