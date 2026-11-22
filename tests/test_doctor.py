from __future__ import annotations

import subprocess

import pytest

from video_translator import doctor
from video_translator.doctor import FAIL, OK, WARN, CheckResult

SECRET = "sk-super-secret-value-12345"


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{cmd[0]} version 7.0\nmore", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)


def test_python_version():
    assert doctor.check_python((3, 12, 1)).status == OK
    assert doctor.check_python((3, 9, 7)).status == FAIL


def test_tool_found_and_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    result = doctor.check_tool("ffmpeg")
    assert result.status == OK and "ffmpeg version 7.0" in result.message

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor.check_tool("ffprobe")
    assert result.status == FAIL and "winget install Gyan.FFmpeg" in result.message


def test_hardware_ok_warn_unknown(monkeypatch):
    gb = 1024**3
    assert doctor.check_hardware(8, 16 * gb).status == OK
    low = doctor.check_hardware(2, 2 * gb)
    assert low.status == WARN and "2.0 GB" in low.message
    monkeypatch.setattr(doctor, "total_ram_bytes", lambda: None)
    unknown = doctor.check_hardware(4)
    assert unknown.status == OK and "unknown" in unknown.message


def test_device_cpu_is_fine():
    result = doctor.check_device("cpu")
    assert result.status == OK and "cpu" in result.message


def test_keys_never_printed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    set_result = doctor.check_key("GEMINI_API_KEY")
    assert set_result.status == OK and set_result.message == "set"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    missing = doctor.check_key("GEMINI_API_KEY")
    assert missing.status == WARN and "AI Studio" in missing.message
    assert SECRET not in set_result.line()


def test_vieneu_library_ok_when_importable():
    result = doctor.check_vieneu("library", importer=lambda name: object())
    assert result.name == "vieneu" and result.status == OK


def test_vieneu_library_warns_with_install_command():
    def missing(name):
        raise ImportError(name)

    result = doctor.check_vieneu("library", importer=missing)
    assert result.status == WARN and "pip install vieneu" in result.message


def test_vieneu_server_reachable_and_not():
    seen = []

    class Conn:
        def close(self):
            seen.append("closed")

    ok = doctor.check_vieneu(
        "server",
        "http://localhost:8123",
        connector=lambda addr, timeout: seen.append(addr) or Conn(),
    )
    assert ok.status == OK and seen[0] == ("localhost", 8123) and seen[1] == "closed"

    def refused(addr, timeout):
        raise ConnectionRefusedError()

    bad = doctor.check_vieneu("server", "http://localhost:8123", connector=refused)
    assert bad.status == WARN and "apps.openai_speech" in bad.message


def test_config_failure(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("chunk_minutes: -3\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(bad))
    result = doctor.check_config()
    assert result.status == FAIL and "chunk_minutes" in result.message


def test_exit_code():
    assert doctor.exit_code([CheckResult("a", OK, ""), CheckResult("b", WARN, "")]) == 0
    assert doctor.exit_code([CheckResult("a", OK, ""), CheckResult("b", FAIL, "")]) == 1


def test_main_output_and_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.delenv("VT_CONFIG", raising=False)
    code = doctor.main()
    out = capsys.readouterr().out
    assert code == 1
    assert SECRET not in out
    lines = [ln for ln in out.splitlines() if ln.startswith("[")]
    assert len(lines) == 9
    assert any(ln.startswith("[FAIL] ffmpeg") for ln in lines)
