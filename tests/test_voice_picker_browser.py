"""The voice picker on the upload page, driven in a real browser."""

from __future__ import annotations

import time

import pytest
import yaml

from tests.browser_support import TRACK_MEDIA_JS, LiveServer, browser_launch_kwargs
from tests.e2e_support import FakeVieNeu
from video_translator.config import AppConfig, PathsConfig
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.vieneu import VieNeuTTS
from video_translator.stages.base import Stage
from video_translator.web.app import create_app
from video_translator.web.voice_samples import DEFAULT_SAMPLE_TEXT

LAUNCH = browser_launch_kwargs()
pytestmark = pytest.mark.skipif(LAUNCH is None, reason="Playwright with Chromium is not available")


class Nothing(Stage):
    name = "nothing"

    def run(self, job, config):
        return job


def wait_until(fn, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_listen_to_voices_then_upload_with_one_and_make_it_default(tmp_path, monkeypatch):
    base = tmp_path / "cfg" / "default.yaml"
    base.parent.mkdir()
    base.write_text("review: true\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(base))

    fake = FakeVieNeu()
    config = AppConfig(
        paths=PathsConfig(
            input=tmp_path / "input", work=tmp_path / "work", output=tmp_path / "output"
        )
    )
    config.vieneu.voice = "Trúc Ly"
    app = create_app(
        config,
        lambda cfg: Pipeline([Nothing()], cfg),
        lambda cfg: VieNeuTTS(config=cfg, transport=fake),
    )
    server = LiveServer(app).start()
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    requests: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**LAUNCH)
            context = browser.new_context(viewport={"width": 1000, "height": 900})
            context.add_init_script(TRACK_MEDIA_JS)
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("request", lambda r: requests.append(r.url))
            page.goto(server.url)
            page.wait_for_selector("#voice-picker")

            # nothing is loaded (or downloaded) until the box is opened
            assert not any("/api/voices" in url for url in requests)
            assert page.locator("#voice-controls").is_hidden()

            page.click("#voice-picker summary")
            page.wait_for_selector("#voice-select option", state="attached")
            options = page.locator("#voice-select option").all_inner_texts()
            assert options == [
                "⭐ Minh Quân — Nam · Bắc",
                "⭐ Mai Anh — Nữ · Bắc · Giọng đọc tự nhiên",
                "Trúc Ly — Nữ · Nam",
            ]
            assert page.locator("#voice-select").input_value() == "Trúc Ly"  # the id, not the label
            assert page.locator("#voice-sample-text").input_value() == DEFAULT_SAMPLE_TEXT
            assert page.locator("#voice-current").inner_text() == "Trúc Ly"

            # hear another voice with the standard sentence
            page.select_option("#voice-select", "Mai Anh")
            assert page.locator("#voice-current").inner_text() == "Mai Anh"
            page.click("#voice-play")
            wait_until(lambda: fake.calls == [(DEFAULT_SAMPLE_TEXT, "Mai Anh")])
            wait_until(lambda: page.evaluate("window.__media.length") == 1)
            assert page.locator("#voice-audio").get_attribute("src").startswith("blob:")
            wait_until(lambda: page.locator("#voice-play").inner_text() == "Play sample")

            # hear the same voice with the user's own sentence
            page.fill("#voice-sample-text", "Tôi muốn nghe câu này")
            page.click("#voice-play")
            wait_until(lambda: fake.calls[-1] == ("Tôi muốn nghe câu này", "Mai Anh"))

            assert page.locator("#voice-error").inner_text() == ""

            # upload with this voice for this video, and make it the default
            page.check("#voice-default")
            page.set_input_files(
                "#file-input",
                {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": b"fake video"},
            )
            page.click("#upload-btn")
            wait_until(lambda: len(app.state.jobs.list_jobs()) == 1)
            job = app.state.jobs.list_jobs()[0]
            assert job.voice == "Mai Anh"
            wait_until(lambda: "default voice" in page.locator("#toast").inner_text())
            saved = yaml.safe_load((base.parent / "local.yaml").read_text(encoding="utf-8"))
            assert saved == {"vieneu": {"voice": "Mai Anh"}}
            assert not page.locator("#voice-default").is_checked()
            context.close()
            browser.close()
    finally:
        server.stop()
        app.state.jobs.shutdown()
    assert errors == [], errors
