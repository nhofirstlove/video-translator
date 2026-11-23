"""``video-translator doctor``: check that this computer is ready to run the tool."""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

OK, WARN, FAIL = "OK", "WARN", "FAIL"

MIN_PYTHON = (3, 10)
MIN_RAM_GB = 4.0

FFMPEG_HINT = "on Windows run `winget install Gyan.FFmpeg`, then reopen the terminal"
KEY_HELP = {
    "GEMINI_API_KEY": "get one in Google AI Studio (https://aistudio.google.com/apikey); it is "
    "the fallback when the free 9Router models fail",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str

    def line(self) -> str:
        return f"[{self.status}] {self.name}: {self.message}"


def check_python(version: tuple[int, ...] | None = None) -> CheckResult:
    version = version or tuple(sys.version_info[:3])
    text = ".".join(str(v) for v in version[:3])
    if tuple(version[:2]) < MIN_PYTHON:
        return CheckResult("Python", FAIL, f"{text} found, need 3.10 or newer")
    return CheckResult("Python", OK, text)


def _version_line(tool: str) -> str:
    try:
        out = subprocess.run(
            [tool, "-version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout
        return out.splitlines()[0].strip() if out.strip() else "version unknown"
    except (OSError, subprocess.SubprocessError):
        return "version unknown"


def check_tool(tool: str) -> CheckResult:
    path = shutil.which(tool)
    if not path:
        return CheckResult(tool, FAIL, f"not found on PATH; {FFMPEG_HINT}")
    return CheckResult(tool, OK, _version_line(tool))


def total_ram_bytes() -> int | None:
    """Total physical memory using only the standard library, or None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        elif sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


def check_hardware(cores: int | None = None, ram_bytes: int | None = None) -> CheckResult:
    cores = cores if cores is not None else os.cpu_count()
    cores_text = f"{cores} CPU cores" if cores else "CPU cores unknown"
    if ram_bytes is None:
        ram_bytes = total_ram_bytes()
    if ram_bytes is None:
        return CheckResult("Hardware", OK, f"{cores_text}, RAM unknown")
    gb = ram_bytes / 1024**3
    text = f"{cores_text}, {gb:.1f} GB RAM"
    if gb < MIN_RAM_GB:
        return CheckResult("Hardware", WARN, f"{text} (less than {MIN_RAM_GB:.0f} GB may be slow)")
    return CheckResult("Hardware", OK, text)


def check_device(pref: str = "auto") -> CheckResult:
    from video_translator.utils.device import resolve_device

    device = resolve_device(pref)
    note = " (fine: this version uses cloud APIs, no GPU needed)" if device == "cpu" else ""
    return CheckResult("Device", OK, f"{device}{note}")


def check_key(var: str) -> CheckResult:
    """Report only whether the key is set, never any part of its value."""
    if os.environ.get(var, "").strip():
        return CheckResult(var, OK, "set")
    return CheckResult(var, WARN, f"missing; {KEY_HELP[var]} and put it in .env")


def check_vieneu(
    mode: str = "library",
    server_url: str = "http://localhost:8000",
    importer: Callable[[str], object] = importlib.import_module,
    connector: Callable[..., object] = socket.create_connection,
) -> CheckResult:
    """Library mode: the ``vieneu`` package imports. Server mode: the server answers."""
    if mode == "server":
        parsed = urlparse(server_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            conn = connector((host, port), timeout=2)
            close = getattr(conn, "close", None)
            if close:
                close()
        except OSError:
            return CheckResult(
                "vieneu",
                WARN,
                f"cannot reach the VieNeu server at {server_url}; start it with "
                "`uv run python -m apps.openai_speech` in the VieNeu-TTS folder",
            )
        return CheckResult("vieneu", OK, f"server mode, {server_url} is reachable")
    try:
        importer("vieneu")
    except Exception:  # not installed, or its own dependencies are broken
        return CheckResult(
            "vieneu",
            WARN,
            "voice package not installed; in the activated virtual environment run "
            "`pip install vieneu`",
        )
    return CheckResult("vieneu", OK, "library mode, voice package installed")


def check_whisper(
    asr: str = "whisper_gemini",
    model: str = "medium",
    importer: Callable[[str], object] = importlib.import_module,
) -> CheckResult:
    """The local speech-timing package (faster-whisper) imports, when it is in use."""
    if asr not in ("whisper", "whisper_gemini"):
        return CheckResult("whisper", OK, f"not used (providers.asr is {asr})")
    try:
        importer("faster_whisper")
    except Exception:
        return CheckResult(
            "whisper",
            WARN,
            "speech-timing package not installed; in the activated virtual environment run "
            "`pip install faster-whisper`",
        )
    return CheckResult(
        "whisper",
        OK,
        f"installed; the {model!r} model downloads on first use (internet needed once)",
    )


def check_ninerouter(
    translation: str = "ninerouter",
    base_url: str = "http://localhost:20128/v1",
    models: tuple[str, ...] = ("free-combo",),
    opener: Callable[..., object] | None = None,
) -> CheckResult:
    """9Router answers, accepts the key and knows the configured models (only when it is used)."""
    if translation != "ninerouter":
        return CheckResult("9Router", OK, f"not used (providers.translation is {translation})")
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("NINEROUTER_API_KEY", "").strip() or "sk_9router"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        response = (opener or urllib.request.urlopen)(request, timeout=3)
        try:
            body = response.read()  # type: ignore[attr-defined]
        except Exception:
            body = b""
        close = getattr(response, "close", None)
        if close:
            close()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return CheckResult(
                "9Router",
                WARN,
                f"9Router refused the key ({exc.code}); copy a key from its dashboard "
                "(http://localhost:20128) and put NINEROUTER_API_KEY=<key> in .env",
            )
        return CheckResult("9Router", WARN, f"9Router at {base_url} answered {exc.code}")
    except Exception:  # not running, wrong address, refused
        return CheckResult(
            "9Router",
            WARN,
            f"cannot reach 9Router at {base_url}; start it with `9router` (then Gemini would "
            "translate everything, if it has a key)",
        )
    try:
        known = [str(m["id"]) for m in json.loads(body)["data"] if isinstance(m, dict)]
    except (ValueError, KeyError, TypeError):
        known = []
    if known:
        missing = [m for m in models if m not in known]
        if len(missing) == len(models):
            shown = ", ".join(known[:8]) + (" ..." if len(known) > 8 else "")
            return CheckResult(
                "9Router",
                WARN,
                f"reachable, but it does not list {', '.join(missing)}; create a combo with "
                "that name in the dashboard (Combos) or set ninerouter.models to a name it "
                f"lists, for example: {shown}",
            )
    return CheckResult(
        "9Router", OK, f"reachable at {base_url}; models tried in order: {', '.join(models)}"
    )


def check_demucs(
    enabled: bool = False, importer: Callable[[str], object] = importlib.import_module
) -> CheckResult:
    """The voice-separation package imports (a warning only when separation is the default)."""
    try:
        importer("demucs")
        installed = True
    except Exception:
        installed = False
    if installed:
        return CheckResult("demucs", OK, "installed; tick the box on the upload form to use it")
    status = WARN if enabled else OK
    return CheckResult(
        "demucs",
        status,
        "not installed (optional: separates the voice from loud music before listening); "
        "to use it, in the activated virtual environment run `pip install demucs`",
    )


def check_config() -> CheckResult:
    from video_translator.config import ConfigError, load_config

    try:
        load_config()
    except ConfigError as exc:
        return CheckResult("Config", FAIL, str(exc))
    return CheckResult("Config", OK, "loaded and valid")


def run_checks() -> list[CheckResult]:
    config_result = check_config()
    device_pref = "auto"
    mode, server_url = "library", "http://localhost:8000"
    asr, whisper_model = "whisper", "medium"
    translation, nr_url, nr_models = "ninerouter", "http://localhost:20128/v1", ("free-combo",)
    separation_on = False
    if config_result.status == OK:
        from video_translator.config import load_config

        cfg = load_config()
        device_pref = cfg.device
        mode, server_url = cfg.vieneu.mode, cfg.vieneu.server_url
        asr, whisper_model = cfg.providers.asr, cfg.whisper.model
        translation = cfg.providers.translation
        nr_url, nr_models = cfg.ninerouter.base_url, tuple(cfg.ninerouter.models)
        separation_on = cfg.separation.enabled
    return [
        check_python(),
        check_tool("ffmpeg"),
        check_tool("ffprobe"),
        check_hardware(),
        check_device(device_pref),
        check_key("GEMINI_API_KEY"),
        check_ninerouter(translation, nr_url, nr_models),
        check_whisper(asr, whisper_model),
        check_vieneu(mode, server_url),
        check_demucs(separation_on),
        config_result,
    ]


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def main() -> int:
    print(f"video-translator doctor ({platform.system()} {platform.release()})")
    results = run_checks()
    for result in results:
        print(result.line())
    return exit_code(results)
