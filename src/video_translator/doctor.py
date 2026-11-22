"""``video-translator doctor``: check that this computer is ready to run the tool."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

OK, WARN, FAIL = "OK", "WARN", "FAIL"

MIN_PYTHON = (3, 10)
MIN_RAM_GB = 4.0

FFMPEG_HINT = "on Windows run `winget install Gyan.FFmpeg`, then reopen the terminal"
KEY_HELP = {
    "GEMINI_API_KEY": "get one in Google AI Studio (https://aistudio.google.com/apikey)",
    "ELEVENLABS_API_KEY": "get one in your ElevenLabs profile (API keys)",
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
    if config_result.status == OK:
        from video_translator.config import load_config

        device_pref = load_config().device
    return [
        check_python(),
        check_tool("ffmpeg"),
        check_tool("ffprobe"),
        check_hardware(),
        check_device(device_pref),
        check_key("GEMINI_API_KEY"),
        check_key("ELEVENLABS_API_KEY"),
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
