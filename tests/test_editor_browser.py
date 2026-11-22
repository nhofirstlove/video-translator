"""The interactive editor in a REAL browser against the REAL app.

The real stages and pipeline run on a synthetic 40 s video; only the lowest transports (Gemini,
VieNeu) are fakes. Skipped when Playwright or Chromium is not available.

The tests share one server and two jobs and run in file order: a finished job ("done") and a
job paused at the review gate before any voice exists ("review"). Screenshots go to
``tests/_screenshots`` only when ``EDITOR_SHOT_DIR`` is set.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.browser_support import TRACK_MEDIA_JS, LiveServer, browser_launch_kwargs
from tests.e2e_support import (
    UTTERANCES,
    install_fakes,
    make_config,
    make_video,
    needs_ffmpeg,
)
from video_translator.models import Job, JobStatus
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.vieneu import VieNeuTTS
from video_translator.stages import build_stages
from video_translator.web.app import create_app

LAUNCH = browser_launch_kwargs()
pytestmark = [
    needs_ffmpeg,
    pytest.mark.skipif(LAUNCH is None, reason="Playwright with Chromium is not available"),
]

SHOT_DIR = os.environ.get("EDITOR_SHOT_DIR")
EDITED_TEXT = "Câu này đã được sửa trong trình duyệt"


class World:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.fakes = install_fakes(monkeypatch, tmp_path)
        self.config = make_config(tmp_path)
        self.config.paths.input.mkdir(parents=True, exist_ok=True)

        def pipeline(job: Job) -> Job:
            return Pipeline(build_stages(self.config), self.config).run(job)

        self.done_id = self._new_job(tmp_path, "done")
        job = Job.load(self.config.paths.work / self.done_id)
        job.review_approved = True
        job = Pipeline(build_stages(self.config), self.config).resume(job)
        assert job.status == JobStatus.DONE, job.error
        self.review_id = self._new_job(tmp_path, "review")
        self.fakes.tts.calls.clear()

        def tts(config):
            return VieNeuTTS(config=config, transport=self.fakes.tts)

        self.app = create_app(self.config, tts_factory=tts)
        self.server = LiveServer(self.app).start()

    def _new_job(self, tmp_path: Path, name: str) -> str:
        video = make_video(self.config.paths.input / f"{name}.mp4")
        job = Job.create(video, self.config.paths.work)
        job = Pipeline(build_stages(self.config), self.config).run(job)
        assert job.status == JobStatus.AWAITING_REVIEW, job.error
        return job.id

    def job(self, job_id: str) -> Job:
        return Job.load(self.config.paths.work / job_id)

    def close(self) -> None:
        self.server.stop()
        self.app.state.jobs.shutdown()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    w = World(tmp_path_factory.mktemp("browser"), monkeypatch)
    yield w
    w.close()
    monkeypatch.undo()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(**LAUNCH)
        yield b
        b.close()


class Editor:
    """One editor page plus everything it logged."""

    def __init__(self, browser, world: World, job_id: str, width=1280, height=800) -> None:
        self.world, self.job_id = world, job_id
        self.context = browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=1
        )
        self.context.add_init_script(TRACK_MEDIA_JS)
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: (
                self.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None
            ),
        )
        self.page.on(
            "response",
            lambda r: (
                self.errors.append(f"HTTP {r.status} {r.request.method} {r.url}")
                if r.status >= 500
                else None
            ),
        )
        self.page.goto(f"{world.server.url}/editor/{job_id}")
        self.page.wait_for_selector("article.line")

    def shot(self, name: str) -> None:
        if SHOT_DIR:
            Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOT_DIR) / f"{name}.png"), full_page=True)

    def line(self, index: int):
        return self.page.locator(f"#line-{index}")

    def toasts(self) -> str:
        return self.page.locator("#ed-toasts").inner_text()

    def close(self) -> None:
        self.context.close()


@pytest.fixture
def make_editor(browser, world):
    opened: list[Editor] = []

    def make(job_id: str, **kw) -> Editor:
        editor = Editor(browser, world, job_id, **kw)
        opened.append(editor)
        return editor

    yield make
    for editor in opened:
        editor.close()
        assert editor.errors == [], editor.errors


# ----- helpers ---------------------------------------------------------------------------


def seg_of(world: World, job_id: str, index: int):
    return world.job(job_id).segments[index]


def wait_until(fn, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def srt_text(world: World, job_id: str) -> str:
    return Path(world.job(job_id).artifacts["srt_output"]).read_text(encoding="utf-8")


# ----- the finished job ------------------------------------------------------------------


def test_done_job_plays_and_subtitles_follow(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    assert page.locator("article.line").count() == len(UTTERANCES)
    assert page.locator("#line-0 .line-idx").inner_text() == "#1"  # numbering is 1-based
    assert page.locator("#ed-title").inner_text() == "done.mp4"
    ed.shot("done_1280")

    page.evaluate("document.getElementById('ed-video').play()")
    wait_until(lambda: page.evaluate("document.getElementById('ed-video').currentTime") > 1.2, 15)
    assert not page.evaluate("document.getElementById('ed-video').paused")
    page.evaluate("document.getElementById('ed-video').pause()")

    # the subtitle overlay shows the line that is active at the video time
    page.evaluate("document.getElementById('ed-video').currentTime = 5")
    wait_until(lambda: "câu thứ 1" in page.locator("#ed-sub-vi").inner_text(), 10, "overlay")
    page.evaluate("document.getElementById('ed-video').currentTime = 3.6")  # a gap
    wait_until(lambda: page.locator("#ed-sub-vi").inner_text().strip() == "", 10, "gap")

    # clicking a line seeks the video to its start (line 5 starts at 16.5 s)
    page.locator("#line-4 .line-dur").click()
    wait_until(
        lambda: abs(page.evaluate("document.getElementById('ed-video').currentTime") - 16.5) < 0.3,
        10,
        "seek",
    )
    assert "is-active" in page.locator("#line-4").get_attribute("class")


def test_edit_text_and_times_change_the_job(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    ed.line(2).locator("textarea.line-vi").fill(EDITED_TEXT)
    ed.line(2).locator("textarea.line-vi").blur()
    wait_until(lambda: seg_of(world, world.done_id, 2).text_vi == EDITED_TEXT, 10, "text saved")
    assert seg_of(world, world.done_id, 2).audio_stale  # the old voice no longer matches
    wait_until(lambda: "out of date" in ed.line(2).inner_text().lower(), 10, "stale label")

    # a bad time is refused with a message and nothing changes
    start_box = page.locator("#ed-start-1")
    start_box.fill("abc")
    start_box.press("Enter")
    wait_until(lambda: ed.line(1).locator(".line-error").inner_text().strip(), 10, "error")
    end_box = page.locator("#ed-end-1")
    end_box.fill("00:04.0")  # before the start (4.5)
    end_box.press("Enter")
    wait_until(lambda: ed.line(1).locator(".line-error").inner_text().strip(), 10, "error 2")
    assert (seg_of(world, world.done_id, 1).start, seg_of(world, world.done_id, 1).end) == (
        4.5,
        7.0,
    )
    ed.shot("done_error")

    # good times: accepted and written to job.json
    end_box.fill("7.4")
    end_box.press("Enter")
    wait_until(lambda: seg_of(world, world.done_id, 1).end == 7.4, 10, "end saved")
    assert ed.line(1).locator(".line-error").inner_text().strip() == ""
    start_box.fill("0:04.6")
    start_box.press("Enter")
    wait_until(lambda: seg_of(world, world.done_id, 1).start == 4.6, 10, "start saved")
    page.reload()
    page.wait_for_selector("article.line")
    assert page.locator("#ed-start-1").input_value() == "00:04.600"  # survives a reload


def test_listen_plays_the_clip(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    ed.line(0).locator("[data-role=listen]").click()
    clip = wait_until(
        lambda: page.evaluate(
            """() => window.__media.filter(m => m.tagName === 'AUDIO' && m.src.includes('/audio'))
                 .map(m => ({src: m.src, paused: m.paused, t: m.currentTime, d: m.duration}))[0]"""
        ),
        10,
        "listen audio",
    )
    assert "/segments/0/audio" in clip["src"]
    wait_until(
        lambda: page.evaluate(
            "window.__media.some(m => m.tagName === 'AUDIO' && (m.currentTime > 0 || m.ended))"
        ),
        10,
        "clip progressing",
    )
    assert world.fakes.tts.calls == []  # listening is free


def test_regenerate_runs_immediately_without_confirmation(make_editor, world):
    ed = make_editor(world.done_id)
    calls_before = len(world.fakes.tts.calls)
    ed.line(2).locator("[data-role=regen]").click()  # one click: no confirmation panel
    assert ed.line(2).locator(".ea-confirm-msg").count() == 0
    wait_until(lambda: len(world.fakes.tts.calls) == calls_before + 1, 20, "tts call")
    wait_until(lambda: not seg_of(world, world.done_id, 2).audio_stale, 20, "stale cleared")
    wait_until(lambda: "voice ready" in ed.line(2).inner_text().lower(), 10, "ready label")
    assert world.fakes.tts.calls[-1][0] == EDITED_TEXT
    assert len(world.fakes.tts.calls) == calls_before + 1  # exactly one call
    page_text = ed.page.inner_text("body").lower()
    assert "quota" not in page_text and "characters" not in page_text


def test_regenerate_provider_error_is_shown_and_keeps_the_old_clip(make_editor, world):
    ed = make_editor(world.done_id)
    text = seg_of(world, world.done_id, 3).text_vi
    before = seg_of(world, world.done_id, 3).aligned_path
    world.fakes.tts.fail_on = text
    try:
        ed.line(3).locator("[data-role=regen]").click()
        wait_until(lambda: "could not speak" in ed.toasts().lower(), 20, "error toast")
        ed.shot("done_voice_error")
    finally:
        world.fakes.tts.fail_on = None
    ed.errors.clear()  # the 502 (and its console line) is the expected failure
    assert seg_of(world, world.done_id, 3).aligned_path == before
    wait_until(lambda: ed.line(3).locator("[data-role=regen]").is_enabled(), 10, "button back")


def test_speed_changes_the_clip_and_reset_goes_back_to_auto(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    calls_before = len(world.fakes.tts.calls)

    def clip_seconds() -> float:
        res = page.request.get(f"{world.server.url}/api/jobs/{world.done_id}/segments/4/audio")
        path = world.config.paths.work / "probe.wav"
        path.write_bytes(res.body())
        from video_translator.utils.ffmpeg import probe_duration

        return probe_duration(path)

    seg = seg_of(world, world.done_id, 4)
    assert seg.speed is None
    ed.line(4).locator("input[data-role=speed]").fill("1.3")
    wait_until(lambda: seg_of(world, world.done_id, 4).speed == 1.3, 20, "speed saved")
    fast = clip_seconds()
    ed.line(4).locator("[data-role=reset]").click()
    wait_until(lambda: seg_of(world, world.done_id, 4).speed is None, 20, "speed reset")
    wait_until(lambda: "auto" in ed.line(4).locator(".ea-readout").inner_text(), 10)
    assert clip_seconds() != pytest.approx(fast, abs=0.02)
    assert len(world.fakes.tts.calls) == calls_before  # speed is free: no new voice


def test_volumes_persist_and_apply_rerenders_without_new_voices(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    calls_before = len(world.fakes.tts.calls)
    job = world.job(world.done_id)
    mp4, srt = Path(job.artifacts["output"]), Path(job.artifacts["srt_output"])
    mp4_before, srt_before = mp4.stat().st_mtime_ns, srt.stat().st_mtime_ns
    assert EDITED_TEXT not in srt.read_text(encoding="utf-8")

    page.locator("#ea-vol-voice").fill("0.8")
    page.locator("#ea-vol-bg").fill("0.6")
    page.locator("#ea-vol-duck").fill("0.1")
    wait_until(lambda: "saved" in page.locator(".ea-save").inner_text().lower(), 10, "saved note")
    wait_until(lambda: world.job(world.done_id).audio_overrides, 10, "overrides on disk")
    overrides = world.job(world.done_id).audio_overrides
    assert overrides == {"voice_volume": 0.8, "background_volume": 0.6, "ducked_volume": 0.1}
    page.reload()
    page.wait_for_selector("article.line")
    assert page.locator("#ea-vol-voice").input_value() == "0.8"  # persisted on the server

    page.locator(".ea-apply .ea-primary").click()
    wait_until(lambda: mp4.stat().st_mtime_ns != mp4_before, 60, "mp4 re-rendered")
    wait_until(lambda: world.job(world.done_id).status == JobStatus.DONE, 60, "job done again")
    assert srt.stat().st_mtime_ns != srt_before
    assert EDITED_TEXT in srt.read_text(encoding="utf-8")
    assert "00:00:04,600" in srt.read_text(encoding="utf-8")  # the edited start time
    assert len(world.fakes.tts.calls) == calls_before  # Apply never speaks
    wait_until(lambda: page.locator(".ea-links a").count() >= 1, 20, "download links")
    ed.shot("done_applied")


def test_job_is_read_only_while_running(make_editor, world):
    import threading

    from video_translator.pipeline import Pipeline
    from video_translator.stages import build_stages
    from video_translator.stages.base import Stage

    gate = threading.Event()
    real = [
        s for s in build_stages(world.config) if s.name in ("subtitles", "align", "mix", "render")
    ]

    class Gated(Stage):
        name = "subtitles"

        def run(self, job, config):
            assert gate.wait(60)
            return real[0].run(job, config)

    manager = world.app.state.jobs
    manager.apply_factory = lambda cfg: Pipeline([Gated(), *real[1:]], cfg)
    ed = make_editor(world.done_id)
    page = ed.page
    page.locator(".ea-apply .ea-primary").click()
    try:
        wait_until(lambda: page.locator("body.readonly").count() == 1, 15, "read-only mode")
        assert page.locator("#ed-banner").is_visible()
        assert page.locator("#line-0 textarea.line-vi").is_disabled()
        assert page.locator("#ed-start-0").is_disabled()
        assert ed.line(0).locator("[data-role=regen]").is_disabled()
        ed.shot("done_running")
        res = page.request.patch(
            f"{world.server.url}/api/jobs/{world.done_id}/segments/0", data={"text_vi": "x y"}
        )
        assert res.status == 409
    finally:
        gate.set()
    wait_until(lambda: page.locator("body.readonly").count() == 0, 60, "editable again")
    wait_until(lambda: world.job(world.done_id).status == JobStatus.DONE, 60)


# ----- the job paused at the review gate (no voice yet) ----------------------------------


def test_review_job_shows_no_voice_and_generate_works(make_editor, world):
    ed = make_editor(world.review_id)
    page = ed.page
    assert page.locator("#ed-status").inner_text().lower() == "needs your review"
    assert "no voice yet" in ed.line(0).inner_text().lower()
    assert ed.line(0).locator("[data-role=listen]").is_disabled()
    assert ed.line(0).locator("input[data-role=speed]").is_disabled()
    assert not page.locator("#ed-dub-toggle").is_enabled()
    ed.shot("review_1280")

    calls_before = len(world.fakes.tts.calls)
    ed.line(0).locator("[data-role=regen]").click()
    wait_until(lambda: len(world.fakes.tts.calls) == calls_before + 1, 20, "tts call")
    wait_until(lambda: "voice ready" in ed.line(0).inner_text().lower(), 20, "voice ready")
    assert seg_of(world, world.review_id, 0).audio_path
    assert ed.line(0).locator("[data-role=listen]").is_enabled()
    assert "no voice yet" in ed.line(1).inner_text().lower()  # the others are untouched


def test_phone_width_layout_has_no_horizontal_scroll(make_editor, world):
    for job_id, name in ((world.done_id, "done"), (world.review_id, "review")):
        ed = make_editor(job_id, width=420)
        ed.page.wait_for_timeout(500)
        overflow = ed.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, overflow
        ed.shot(f"{name}_420")


def test_main_page_links_to_the_editor(make_editor, world):
    ed = make_editor(world.done_id)  # reuses the error/console collection of the helper
    page = ed.page
    page.goto(f"{world.server.url}/")
    for job_id in (world.done_id, world.review_id):
        link = page.locator(f"a[href='/editor/{job_id}']")
        wait_until(lambda link=link: link.count() == 1, 15, "Open editor link")
        assert link.inner_text() == "Open editor"
    link = page.locator(f"a[href='/editor/{world.review_id}']")
    link.click()
    page.wait_for_selector("article.line")
    assert page.url.endswith(f"/editor/{world.review_id}")


def test_script_load_order_does_not_matter(browser, world):
    """editor_audio.js loaded BEFORE editor.js (and after) must give the same mounted page."""
    results = []
    for swapped in (False, True):
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
        page.on(
            "console",
            lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None,
        )

        def reorder(route, swapped=swapped):
            body = route.fetch().text()
            first, second = "/static/editor.js", "/static/editor_audio.js"
            tag = '<script src="{}"></script>'
            if swapped:
                body = body.replace(tag.format(first), "@@A@@").replace(tag.format(second), "@@B@@")
                body = body.replace("@@A@@", tag.format(second)).replace("@@B@@", tag.format(first))
            route.fulfill(body=body, content_type="text/html")

        page.route(f"**/editor/{world.done_id}", reorder)
        page.goto(f"{world.server.url}/editor/{world.done_id}")
        page.wait_for_selector("article.line")
        wait_until(lambda page=page: page.locator(".ea-panel").count() == 1, 10, "panel")
        wait_until(lambda page=page: page.locator(".ea-line").count() == len(UTTERANCES), 10)
        results.append(page.locator("#ea-vol-voice").input_value())
        assert errors == [], errors
        context.close()
    assert results[0] == results[1]


# ----- the blur box ---------------------------------------------------------------------------


def test_blur_box_can_be_switched_on_dragged_and_resized(make_editor, world):
    ed = make_editor(world.review_id)
    page = ed.page
    assert page.locator(".eb-box").is_hidden()  # off by default
    page.evaluate("document.getElementById('ed-video').load()")
    wait_until(lambda: page.evaluate("document.getElementById('ed-video').videoWidth") > 0, 15)

    page.locator("#eb-enabled").check()
    box = page.locator(".eb-box")
    wait_until(lambda: box.is_visible(), 10, "box visible")
    wait_until(lambda: world.job(world.review_id).blur_overrides.get("enabled") is True, 10)
    page.locator("#eb-width").fill("100")  # the test video is tiny; make the box smaller than it
    page.locator("#eb-height").fill("30")
    wait_until(lambda: world.job(world.review_id).blur_overrides.get("height") == 30, 10, "size")
    before = world.job(world.review_id).blur_overrides

    # drag the box down by 20 screen pixels: the saved y (video pixels) gets bigger
    box.scroll_into_view_if_needed()
    rect = box.bounding_box()
    page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
    page.mouse.down()
    page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2 + 20, steps=5)
    page.mouse.up()
    wait_until(lambda: world.job(world.review_id).blur_overrides["y"] > before["y"], 10, "moved")
    wait_until(  # the server keeps whole even pixels; the page shows what was saved
        lambda: (
            int(page.locator("#eb-y").input_value())
            == world.job(world.review_id).blur_overrides["y"]
        ),
        10,
        "y shown",
    )

    # resize with the south-east handle: bigger box
    page.locator(".eb-se").scroll_into_view_if_needed()
    handle = page.locator(".eb-se").bounding_box()
    hx, hy = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(hx, hy)
    page.mouse.down()
    page.mouse.move(hx, hy - 1, steps=2)
    page.mouse.up()
    page.locator("#eb-width").fill("60")
    wait_until(lambda: world.job(world.review_id).blur_overrides["width"] == 60, 10, "typed width")
    ed.shot("blur_box")

    # the settings survive a reload; "use the default again" clears them
    page.reload()
    page.wait_for_selector("article.line")
    wait_until(lambda: page.locator("#eb-width").input_value() == "60", 10, "reloaded")
    page.locator("#eb-strength").fill("90")
    wait_until(lambda: world.job(world.review_id).blur_overrides["strength"] == 90, 10)
    page.get_by_role("button", name="Use the default again").click()
    wait_until(lambda: world.job(world.review_id).blur_overrides == {}, 10, "reset")
    wait_until(lambda: page.locator("#eb-enabled").is_checked() is False, 10, "unchecked")


# ----- the timeline under the video -------------------------------------------------------


def _drag(page, from_xy, to_xy, steps=6):
    page.mouse.move(*from_xy)
    page.mouse.down()
    page.mouse.move(*to_xy, steps=steps)
    page.mouse.up()


def test_timeline_has_tracks_blocks_and_waveforms(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-voice")
    assert page.locator(".tl-voice").count() == len(UTTERANCES)
    assert page.locator(".tl-subs").count() == len(UTTERANCES)
    names = page.locator(".tl-row .tl-name strong").all_inner_texts()
    assert names == ["Vietnamese voice", "Background music", "Subtitles", "Blur"]
    # one blur bar for the whole video
    assert page.locator(".tl-blur-bar").count() == 1

    def drawn(selector):
        return page.evaluate(
            """(sel) => { const c = document.querySelector(sel); if (!c || c.hidden) return 0;
              const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
              let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; }""",
            selector,
        )

    wait_until(lambda: drawn(".tl-bg-wave") > 50, 15, "background waveform")
    wait_until(lambda: drawn(".tl-voice[data-index='0'] canvas") > 20, 15, "voice waveform")
    ed.shot("timeline")
    if SHOT_DIR:
        panel = page.locator("#tracks-panel")
        panel.scroll_into_view_if_needed()
        panel.screenshot(path=str(Path(SHOT_DIR) / "timeline_panel.png"))


def test_timeline_voice_blocks_without_a_voice_are_marked(make_editor, world):
    ed = make_editor(world.review_id)
    ed.page.wait_for_selector(".tl-voice")
    segs = world.job(world.review_id).segments
    without = sum(1 for s in segs if not (s.audio_path and Path(s.audio_path).is_file()))
    assert ed.page.locator(".tl-voice.noclip").count() == without
    assert ed.page.locator(".tl-voice").count() == len(segs)


def test_timeline_click_selects_the_line_and_jumps(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-subs")
    page.locator(".tl-subs[data-index='4']").click()
    wait_until(lambda: "is-active" in (ed.line(4).get_attribute("class") or ""), 10, "selected")
    start = seg_of(world, world.done_id, 4).start
    t = page.evaluate("document.getElementById('ed-video').currentTime")
    assert abs(t - start) < 0.2
    assert page.locator(".tl-voice[data-index='4'].is-active").count() == 1
    # clicking the ruler moves the video
    box = page.locator(".tl-ruler-lane").bounding_box()
    page.mouse.click(box["x"] + 100, box["y"] + 12)
    wait_until(
        lambda: abs(page.evaluate("document.getElementById('ed-video').currentTime") - t) > 0.5, 10
    )


def test_timeline_zoom_in_out_and_fit(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-voice")

    def pps():
        return float(
            page.evaluate(
                "getComputedStyle(document.querySelector('.tl-inner')).getPropertyValue('--pps')"
            )
        )

    before = pps()
    page.get_by_role("button", name="Zoom in").click()
    assert pps() == pytest.approx(before * 1.5, rel=0.01)
    page.get_by_role("button", name="Zoom out").click()
    page.get_by_role("button", name="Zoom out").click()
    assert pps() < before
    page.get_by_role("button", name="Fit", exact=True).click()
    inner = page.locator(".tl-inner").bounding_box()["width"]
    scroll = page.locator(".tl-scroll").bounding_box()["width"]
    assert inner <= scroll + 30  # the whole video fits without scrolling


def test_timeline_mute_and_hide_buttons_only_change_the_preview(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-voice")
    before = world.job(world.done_id).model_dump(mode="json")["segments"]

    page.locator(".tl-row-subs .tl-btn").click()
    assert page.locator("#ed-stage.tl-hide-subs").count() == 1
    assert page.locator(".tl-row-subs .tl-btn").inner_text() == "Hidden"
    page.locator(".tl-row-blur .tl-btn").click()
    assert page.locator("#ed-stage.tl-hide-blur").count() == 1
    page.locator(".tl-row-voice .tl-btn").click()
    page.locator(".tl-row-music .tl-btn").click()
    state = page.evaluate("EditorAudio._debug().mute")
    assert state == {"voice": True, "bg": True}
    assert page.locator(".tl-row-music.is-off").count() == 1
    # the rest of the page still works and the same buttons turn everything back on
    for kind in ("subs", "blur", "voice", "music"):
        page.locator(f".tl-row-{kind} .tl-btn").click()
    assert page.locator("#ed-stage.tl-hide-subs").count() == 0
    assert page.evaluate("EditorAudio._debug().mute") == {"voice": False, "bg": False}
    assert world.job(world.done_id).model_dump(mode="json")["segments"] == before  # nothing saved


def test_timeline_drag_block_moves_the_line_and_edges_change_start_and_end(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-subs")
    i = 6
    s0 = seg_of(world, world.done_id, i)
    pps = float(
        page.evaluate(
            "getComputedStyle(document.querySelector('.tl-inner')).getPropertyValue('--pps')"
        )
    )
    block = page.locator(f".tl-subs[data-index='{i}']")
    block.scroll_into_view_if_needed()
    rect = block.bounding_box()
    cx, cy = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

    # move right by about half a second
    _drag(page, (cx, cy), (cx + 0.5 * pps, cy))
    wait_until(lambda: seg_of(world, world.done_id, i).start > s0.start + 0.3, 10, "moved")
    s1 = seg_of(world, world.done_id, i)
    assert s1.end - s1.start == pytest.approx(s0.end - s0.start, abs=0.01)  # same length
    assert s1.start == pytest.approx(s0.start + 0.5, abs=0.15)
    # the voice block shows the same line at the same place
    voice = page.locator(f".tl-voice[data-index='{i}']").bounding_box()
    moved = page.locator(f".tl-subs[data-index='{i}']").bounding_box()
    assert voice["x"] == pytest.approx(moved["x"], abs=1.5)

    # drag the right edge a little to the right: the end changes, the start does not
    rect = page.locator(f".tl-subs[data-index='{i}']").bounding_box()
    ex, ey = rect["x"] + rect["width"] - 3, rect["y"] + rect["height"] / 2
    _drag(page, (ex, ey), (ex + 0.3 * pps, ey))
    wait_until(lambda: seg_of(world, world.done_id, i).end > s1.end + 0.15, 10, "end moved")
    s2 = seg_of(world, world.done_id, i)
    assert s2.start == s1.start

    # drag the left edge to the right: the start changes, the end does not
    rect = page.locator(f".tl-subs[data-index='{i}']").bounding_box()
    lx, ly = rect["x"] + 3, rect["y"] + rect["height"] / 2
    _drag(page, (lx, ly), (lx + 0.3 * pps, ly))
    wait_until(lambda: seg_of(world, world.done_id, i).start > s2.start + 0.15, 10, "start moved")
    assert seg_of(world, world.done_id, i).end == s2.end
    # the text boxes in the line list show the saved times
    wait_until(
        lambda: (
            page.locator(f"#ed-start-{i}").input_value()
            == Editor_time(seg_of(world, world.done_id, i).start)
        ),
        10,
        "time box updated",
    )


def Editor_time(sec: float) -> str:
    ms = round(sec * 1000)
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{ms % 1000:03d}"


def test_timeline_drag_cannot_overlap_neighbours_and_escape_cancels(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    page.wait_for_selector(".tl-voice")
    i = 3
    s0 = seg_of(world, world.done_id, i)
    nxt = seg_of(world, world.done_id, i + 1)
    pps = float(
        page.evaluate(
            "getComputedStyle(document.querySelector('.tl-inner')).getPropertyValue('--pps')"
        )
    )
    block = page.locator(f".tl-voice[data-index='{i}']")
    block.scroll_into_view_if_needed()
    rect = block.bounding_box()
    cx, cy = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

    # Escape while dragging: nothing is saved
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 1.0 * pps, cy, steps=5)
    page.keyboard.press("Escape")
    page.mouse.up()
    time.sleep(0.6)
    assert seg_of(world, world.done_id, i).start == s0.start

    # dragging far to the right stops at the next line
    _drag(page, (cx, cy), (cx + 4 * pps, cy))
    wait_until(lambda: seg_of(world, world.done_id, i).start > s0.start + 0.1, 10, "moved")
    moved = seg_of(world, world.done_id, i)
    assert moved.end <= nxt.start + 1e-6
    assert ed.errors == []


# ----- player without the browser's own controls, blur outline on click, 3/4 + 1/4 layout ----


def test_player_has_no_browser_controls_but_a_play_button_and_click_to_play(make_editor, world):
    ed = make_editor(world.done_id)
    page = ed.page
    assert page.evaluate("document.getElementById('ed-video').controls") is False
    play = page.locator("#ed-play")
    assert play.inner_text() == "Play"
    play.click()
    wait_until(
        lambda: not page.evaluate("document.getElementById('ed-video').paused"), 10, "playing"
    )
    wait_until(lambda: play.inner_text() == "Pause", 10, "label Pause")
    play.click()
    wait_until(lambda: page.evaluate("document.getElementById('ed-video').paused"), 10, "paused")
    wait_until(lambda: play.inner_text() == "Play", 10, "label Play")
    page.locator("#ed-video").click()  # clicking the picture plays too
    wait_until(
        lambda: not page.evaluate("document.getElementById('ed-video').paused"), 10, "playing 2"
    )
    page.locator("#ed-video").click()
    wait_until(lambda: page.evaluate("document.getElementById('ed-video').paused"), 10, "paused 2")


def test_blur_outline_and_handles_show_only_when_the_box_is_clicked(make_editor, world):
    ed = make_editor(world.review_id)
    page = ed.page
    page.evaluate("document.getElementById('ed-video').load()")
    wait_until(lambda: page.evaluate("document.getElementById('ed-video').videoWidth") > 0, 15)
    page.locator("#eb-enabled").check()
    box = page.locator(".eb-box")
    wait_until(lambda: box.is_visible(), 10, "box visible")
    handle = page.locator(".eb-se")
    # switching the box on selects it, so it can be seen where it is
    assert "eb-selected" in box.get_attribute("class") and handle.is_visible()
    # clicking somewhere else hides the outline and the handles; the blur stays
    page.locator("#ed-title").click()
    assert "eb-selected" not in box.get_attribute("class")
    assert handle.is_hidden() and box.is_visible()
    # clicking the box brings them back, Escape hides them again
    box.click()
    assert "eb-selected" in box.get_attribute("class") and handle.is_visible()
    page.keyboard.press("Escape")
    assert "eb-selected" not in box.get_attribute("class")
    page.locator("#eb-enabled").uncheck()
    wait_until(lambda: world.job(world.review_id).blur_overrides.get("enabled") is False, 10)


def test_layout_gives_the_video_three_quarters_and_the_lines_one_quarter(make_editor, world):
    ed = make_editor(world.done_id, width=1600, height=900)
    page = ed.page
    left = page.locator(".ed-left").bounding_box()["width"]
    right = page.locator(".ed-right").bounding_box()["width"]
    assert left / right == pytest.approx(3.0, rel=0.08)
    ed.shot("layout_1600")
