"""Static checks for the editor audio front end (no browser needed)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "video_translator" / "web" / "static"
JS = STATIC / "editor_audio.js"
CSS = STATIC / "editor_audio.css"


def test_files_exist() -> None:
    assert JS.is_file()
    assert CSS.is_file()


def test_exposes_editor_audio_api() -> None:
    src = JS.read_text(encoding="utf-8")
    assert "window.EditorAudio" in src
    assert re.search(r"mountLine\s*:", src)
    assert re.search(r"mountPanel\s*:", src)
    assert "Editor.mountAudio" in src


@pytest.mark.parametrize(
    "pattern",
    [
        r"innerHTML",
        r"outerHTML",
        r"insertAdjacentHTML",
        r"document\.write",
        r"\beval\s*\(",
        r"new\s+Function",
        r"window\.confirm|\bconfirm\s*\(",
        r"https?://",
        r"//cdn\.",
    ],
)
def test_no_unsafe_or_external_code(pattern: str) -> None:
    assert not re.search(pattern, JS.read_text(encoding="utf-8")), pattern
    if pattern.startswith("http") or pattern.startswith("//cdn"):
        assert not re.search(pattern, CSS.read_text(encoding="utf-8")), pattern


def test_css_is_prefixed_and_local() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(" not in css
    selectors = re.findall(r"(?m)^\s*([.#][^{}@]+?)\s*\{", css)
    assert selectors
    assert all(".ea-" in s for s in selectors), [s for s in selectors if ".ea-" not in s]


def test_uses_contract_endpoints() -> None:
    src = JS.read_text(encoding="utf-8")
    for needle in (
        "/regenerate",
        "/speed",
        "/audio-settings",
        "/apply",
        "/download/srt",
        "/download/video",
    ):
        assert needle in src, needle


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_syntax_check() -> None:
    result = subprocess.run(
        ["node", "--check", str(JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
