"""Static checks for the editor page (no browser needed)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "src" / "video_translator" / "web"
TEMPLATE = WEB / "templates" / "editor.html"
JS = WEB / "static" / "editor.js"
CSS = WEB / "static" / "editor.css"


def test_files_exist() -> None:
    for path in (TEMPLATE, JS, CSS):
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip()


def test_template_loads_scripts_in_order_and_has_hooks() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index("/static/editor.js") < html.index("/static/editor_audio.js")
    assert "/static/editor.css" in html
    for needle in (
        'id="audio-panel"',
        'id="ed-video"',
        'id="ed-lines"',
        'id="ed-toasts"',
        'aria-live="polite"',
    ):
        assert needle in html, needle


def test_no_external_urls() -> None:
    for path in (TEMPLATE, JS, CSS):
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        ), path


def test_js_has_no_unsafe_dom_or_eval() -> None:
    js = JS.read_text(encoding="utf-8")
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert banned not in js, banned


def test_js_exposes_the_contract() -> None:
    js = JS.read_text(encoding="utf-8")
    assert "window.Editor" in js
    for name in (
        "jobId",
        "state",
        "video",
        "api",
        "updateSegment",
        "refresh",
        "on",
        "toast",
        "formatTime",
        "parseTime",
        "mountAudio",
    ):
        assert re.search(rf"\b{name}\s*:", js), name
    for event in ("ready", "segment-updated", "seek", "play", "pause", "refreshed"):
        assert f'emit("{event}"' in js, event
    assert "EditorAudio" in js and "mountLine" in js and "mountPanel" in js
    assert 'class: "line-audio"' in js and "data-index" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editor_js_parses_with_node() -> None:
    result = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_time_helpers_in_node() -> None:
    script = (
        "global.window={};global.location={pathname:'/editor/abcdef123456'};"
        "const stub=()=>({style:{setProperty(){}},classList:{add(){},remove(){},toggle(){}},"
        "addEventListener(){},setAttribute(){},removeAttribute(){},appendChild(){},"
        "getBoundingClientRect(){return{}},checked:false,hidden:false});"
        "global.document={readyState:'loading',getElementById:stub,addEventListener(){},createElement:stub,"
        "createTextNode:stub,body:stub(),title:''};"
        f"require({str(JS)!r});const E=window.Editor;"
        "const out=[E.formatTime(75.4),E.formatTime(3725.005),E.parseTime('1:15.4'),"
        "E.parseTime('75,4'),"
        "E.parseTime('abc'),E.jobId];console.log(JSON.stringify(out));"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '["01:15.400","1:02:05.005",75.4,75.4,null,"abcdef123456"]'
