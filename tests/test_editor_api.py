"""Tests for the editor API (src/video_translator/web/editor_api.py) with real ffmpeg.

The voice engine is replaced by the fake VieNeu transport of ``e2e_support``; nothing
here touches the network.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.e2e_support import (
    BED_FREQS,
    VOICE_FREQ,
    FakeVieNeu,
    make_config,
    make_video,
    needs_ffmpeg,
    tone_amplitude,
)
from video_translator.models import Job, JobStatus, Segment
from video_translator.pipeline import Pipeline
from video_translator.providers.tts.vieneu import VieNeuTTS
from video_translator.stages.align import rebuild_voice_track
from video_translator.stages.base import Stage
from video_translator.synth_line import synthesize_line
from video_translator.utils.ffmpeg import probe_duration, run_ffmpeg
from video_translator.web.app import create_app

pytestmark = needs_ffmpeg

LINES = [
    ("第0句话", "Đây là câu thứ 0 của đoạn thử nghiệm"),
    ("第1句话", "Đây là câu thứ 1 của đoạn thử nghiệm"),
    ("第2句话", "Đây là câu thứ 2 của đoạn thử nghiệm"),
    ("第3句话", "Đây là câu thứ 3 của đoạn thử nghiệm"),
    ("第4句话", "Đây là câu thứ 4 của đoạn thử nghiệm"),
]
LINE_CHARS = len(LINES[0][1])
VIDEO_SECONDS = 40
ALL_STAGES = [
    "extract_audio",
    "transcribe",
    "translate",
    "review",
    "subtitles",
    "synthesize",
    "align",
    "mix",
    "render",
]


class Env:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.config = make_config(tmp_path)
        self.fake = FakeVieNeu(tmp_path / "fake_tts")
        self.client = TestClient(create_app(self.config, tts_factory=self._tts))
        self.manager = self.client.app.state.jobs
        # A real synthesize stage would reach the registry: make any such call visible.
        from video_translator.providers.tts import vieneu

        monkeypatch.setattr(vieneu, "_LibraryTransport", lambda precision="fp32": self.fake)

    def _tts(self, config):
        return VieNeuTTS(config=config, transport=self.fake)

    def make_job(
        self, status: JobStatus = JobStatus.DONE, with_audio: tuple[int, ...] = (0, 1, 2, 3)
    ) -> str:
        """A finished-style job: 5 lines (4k+0.5 .. 4k+3.0); lines in ``with_audio`` have clips."""
        config = self.config
        config.paths.input.mkdir(parents=True, exist_ok=True)
        video = make_video(config.paths.input / "clip.mp4", VIDEO_SECONDS)
        job = Job.create(video, config.paths.work)
        wav = job.work_dir / "audio.wav"
        run_ffmpeg(["-i", video, "-vn", "-ar", "44100", "-ac", "2", wav], desc="extract")
        job.artifacts["audio"] = wav
        job.segments = [
            Segment(start=4 * k + 0.5, end=4 * k + 3.0, text_zh=zh, text_vi=vi)
            for k, (zh, vi) in enumerate(LINES)
        ]
        for k in with_audio:
            synthesize_line(job, k, config, self._tts)
        rebuild_voice_track(job)
        job.status = status
        job.completed_stages = ALL_STAGES if status == JobStatus.DONE else ALL_STAGES[:3]
        job.review_approved = status == JobStatus.DONE
        job.save()
        return job.id

    def job(self, job_id: str) -> Job:
        return Job.load(self.config.paths.work / job_id)

    def seg_url(self, job_id: str, index: int, tail: str = "") -> str:
        return f"/api/jobs/{job_id}/segments/{index}{tail}"

    def wait_done(self, job_id: str) -> dict:
        assert self.manager.wait_idle(60), "apply did not finish"
        return self.client.get(f"/api/jobs/{job_id}").json()


@pytest.fixture
def env(tmp_path, monkeypatch):
    environment = Env(tmp_path, monkeypatch)
    with environment.client:
        yield environment
        environment.manager.shutdown()


def clip_duration(env: Env, job_id: str, index: int, tmp_path: Path) -> float:
    res = env.client.get(env.seg_url(job_id, index, "/audio"))
    assert res.status_code == 200
    path = tmp_path / f"clip_{index}_{len(res.content)}.wav"
    path.write_bytes(res.content)
    return probe_duration(path)


# ----- editor data -----------------------------------------------------------------------


def test_editor_data_shape(env):
    job_id = env.make_job()
    data = env.client.get(f"/api/jobs/{job_id}/editor").json()
    assert data["job"]["id"] == job_id
    assert data["job"]["status"] == "done"
    assert data["job"]["input_name"] == "clip.mp4"
    assert data["duration"] == pytest.approx(VIDEO_SECONDS, abs=0.5)
    assert data["media"] == {"video": True, "dub_video": False, "voice_track": True}
    assert len(data["segments"]) == 5
    first, last = data["segments"][0], data["segments"][4]
    assert first["has_audio"] is True and last["has_audio"] is False
    assert first["audio_stale"] is False and first["speed"] is None
    assert first["chars"] == LINE_CHARS
    assert first["text_zh"] == "第0句话" and first["flags"] == []
    assert data["audio"] == {"voice_volume": 1.0, "background_volume": 1.0, "ducked_volume": 0.25}
    assert data["budget"] is None


def test_unknown_or_invalid_job(env):
    assert env.client.get("/api/jobs/aaaaaaaaaaaa/editor").status_code == 404
    res = env.client.get("/api/jobs/not-an-id/editor")
    assert res.status_code == 404 and isinstance(res.json()["detail"], str)
    assert (
        env.client.patch("/api/jobs/aaaaaaaaaaaa/segments/0", json={"text_vi": "x"}).status_code
        == 404
    )


# ----- PATCH -----------------------------------------------------------------------------


def test_patch_times_and_text(env):
    job_id = env.make_job()
    url = env.seg_url(job_id, 1)
    res = env.client.patch(url, json={"start": 5.0, "end": 7.5})
    assert res.status_code == 200
    body = res.json()
    assert (body["start"], body["end"]) == (5.0, 7.5)
    assert "needs_realign" in body["flags"]
    assert body["audio_stale"] is False and body["has_audio"] is True
    saved = env.job(job_id).segments[1]
    assert (saved.start, saved.end) == (5.0, 7.5)

    res = env.client.patch(url, json={"text_vi": "  Xin chào các bạn  "})
    body = res.json()
    assert body["text_vi"] == "Xin chào các bạn"
    assert body["audio_stale"] is True
    assert body["chars"] == len("Xin chào các bạn")
    assert env.job(job_id).segments[1].audio_stale is True

    # the same text again does not change anything else, a line without a clip is not stale
    res = env.client.patch(env.seg_url(job_id, 4), json={"text_vi": "Câu mới"})
    assert res.status_code == 200
    assert res.json()["audio_stale"] is False and res.json()["has_audio"] is False


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"start": -1.0}, "negative"),
        ({"start": 6.0, "end": 6.0}, "last at least"),
        ({"start": 5.0, "end": 5.1}, "last at least"),
        ({"end": 41.0}, "after the end of the video"),
        ({"text_vi": "   "}, "cannot be empty"),
        ({}, "Nothing to change"),
        ({"start": 2.0}, "previous line"),  # line 1 starts at 4.5, line 0 ends at 3.0
        ({"end": 9.0}, "next line"),  # line 2 starts at 8.5
        ({"start": "abc"}, "start"),
    ],
)
def test_patch_validation(env, payload, fragment):
    job_id = env.make_job()
    before = env.job(job_id).segments[1].model_dump()
    res = env.client.patch(env.seg_url(job_id, 1), json=payload)
    assert res.status_code == 422, res.text
    assert isinstance(res.json()["detail"], str)
    assert fragment in res.json()["detail"]
    assert env.job(job_id).segments[1].model_dump() == before


def test_patch_allows_tiny_overlap_and_bad_index(env):
    job_id = env.make_job()
    res = env.client.patch(env.seg_url(job_id, 1), json={"start": 2.96})
    assert res.status_code == 200
    assert env.client.patch(env.seg_url(job_id, 9), json={"text_vi": "x"}).status_code == 404
    assert env.client.patch(env.seg_url(job_id, -1), json={"text_vi": "x"}).status_code == 404


# ----- 409 while running -----------------------------------------------------------------


class GateStage(Stage):
    name = "gate"

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    def run(self, job, config):
        assert self.gate.wait(20)
        job.segments = [Segment(start=0.0, end=2.0, text_zh="a", text_vi="b")]
        return job


def test_edits_refused_while_running(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    gate = threading.Event()
    app = create_app(config, lambda cfg: Pipeline([GateStage(gate)], cfg))
    with TestClient(app) as client:
        job_id = client.post(
            "/api/jobs", files={"file": ("clip.mp4", b"video", "video/mp4")}
        ).json()["id"]
        try:
            for _ in range(500):
                if client.get(f"/api/jobs/{job_id}").json()["running"]:
                    break
                threading.Event().wait(0.01)
            calls = [
                ("patch", f"/api/jobs/{job_id}/segments/0", {"text_vi": "x"}),
                ("post", f"/api/jobs/{job_id}/segments/0/regenerate", {"confirm": True}),
                ("post", f"/api/jobs/{job_id}/segments/0/speed", {"speed": 1.1}),
                (
                    "put",
                    f"/api/jobs/{job_id}/audio-settings",
                    {"voice_volume": 1, "background_volume": 1, "ducked_volume": 0.2},
                ),
                ("post", f"/api/jobs/{job_id}/apply", None),
            ]
            for method, url, body in calls:
                res = getattr(client, method)(url, **({"json": body} if body is not None else {}))
                assert res.status_code == 409, (url, res.text)
                assert isinstance(res.json()["detail"], str)
            assert client.get(f"/api/jobs/{job_id}/editor").status_code == 200  # reading is fine
        finally:
            gate.set()
            app.state.jobs.wait_idle(10)
            app.state.jobs.shutdown()


def test_edits_refused_for_pending_job(env):
    job = Job.create(env.tmp_path / "x.mp4", env.config.paths.work)
    job.save()
    res = env.client.patch(f"/api/jobs/{job.id}/segments/0", json={"text_vi": "x"})
    assert res.status_code == 409


# ----- cost and regenerate ---------------------------------------------------------------


def test_cost_is_free_and_regenerate_runs_immediately(env):
    job_id = env.make_job()
    calls = len(env.fake.calls)

    cost = env.client.get(env.seg_url(job_id, 4, "/cost")).json()
    assert cost == {
        "chars": LINE_CHARS,
        "remaining": None,
        "allowed": True,
        "message": "Free: runs on your computer; may take a few seconds.",
    }

    assert env.client.get(env.seg_url(job_id, 4, "/audio")).status_code == 404
    assert env.client.get(env.seg_url(job_id, 4, "/audio")).json() == {"detail": "no voice yet"}

    # no confirmation is needed (a body with confirm is still accepted)
    res = env.client.post(env.seg_url(job_id, 4, "/regenerate"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_audio"] is True and body["audio_stale"] is False
    assert len(env.fake.calls) == calls + 1
    assert env.fake.calls[-1] == (LINES[4][1], "Mai Anh")
    saved = env.job(job_id).segments[4]
    assert saved.audio_path and Path(saved.audio_path).name == "seg_00004.wav"
    assert saved.aligned_path and Path(saved.aligned_path).is_file()
    assert env.client.get(env.seg_url(job_id, 4, "/audio")).status_code == 200
    assert env.client.get(f"/api/jobs/{job_id}/editor").json()["budget"] is None

    res = env.client.post(env.seg_url(job_id, 4, "/regenerate"), json={"confirm": True})
    assert res.status_code == 200
    assert len(env.fake.calls) == calls + 2


def test_regenerate_stale_line_clears_flag(env):
    job_id = env.make_job()
    env.client.patch(env.seg_url(job_id, 0), json={"text_vi": "Một câu hoàn toàn mới ở đây nhé"})
    assert env.client.get(f"/api/jobs/{job_id}/editor").json()["segments"][0]["audio_stale"]
    res = env.client.post(env.seg_url(job_id, 0, "/regenerate"), json={"confirm": True})
    assert res.status_code == 200
    assert res.json()["audio_stale"] is False
    assert env.fake.calls[-1][0] == "Một câu hoàn toàn mới ở đây nhé"
    assert env.job(job_id).segments[0].audio_stale is False


def test_regenerate_provider_error_is_a_plain_502_and_changes_nothing(env):
    job_id = env.make_job()
    env.fake.fail_on = LINES[4][1]
    res = env.client.post(env.seg_url(job_id, 4, "/regenerate"))
    assert res.status_code == 502
    assert "fake voice engine broke" in res.json()["detail"]
    assert env.job(job_id).segments[4].audio_path is None


def test_regenerate_without_voice_package_explains(tmp_path, monkeypatch):
    import sys

    from video_translator.providers.tts import vieneu

    vieneu.reset_shared_model()
    monkeypatch.setitem(sys.modules, "vieneu", None)  # `import vieneu` fails
    config = make_config(tmp_path)
    with TestClient(create_app(config)) as client:  # default (real) provider, never reaches the net
        job = Job.create(tmp_path / "x.mp4", config.paths.work)
        job.segments = [Segment(start=0, end=2, text_zh="a", text_vi="Xin chào")]
        job.status = JobStatus.DONE
        job.save()
        res = client.post(f"/api/jobs/{job.id}/segments/0/regenerate")
        assert res.status_code == 502
        assert "pip install vieneu" in res.json()["detail"]
        client.app.state.jobs.shutdown()


# ----- speed -----------------------------------------------------------------------------


def test_speed_changes_clip_duration(env, tmp_path):
    job_id = env.make_job()
    calls_before = len(env.fake.calls)
    normal = clip_duration(env, job_id, 1, tmp_path)
    res = env.client.post(env.seg_url(job_id, 1, "/speed"), json={"speed": 1.5})
    assert res.status_code == 200 and res.json()["speed"] == 1.5
    fast = clip_duration(env, job_id, 1, tmp_path)
    assert fast == pytest.approx(normal / 1.5, rel=0.05)
    assert env.job(job_id).segments[1].speed == 1.5

    slow = None
    res = env.client.post(env.seg_url(job_id, 1, "/speed"), json={"speed": 0.8})
    assert res.status_code == 200
    slow = clip_duration(env, job_id, 1, tmp_path)
    assert slow == pytest.approx(normal / 0.8, rel=0.05)

    # back to automatic
    res = env.client.post(env.seg_url(job_id, 1, "/speed"), json={"speed": None})
    assert res.json()["speed"] is None
    assert clip_duration(env, job_id, 1, tmp_path) == pytest.approx(normal, rel=0.05)

    # no new voice was made
    assert len(env.fake.calls) == calls_before


def test_speed_is_trimmed_to_the_slot_and_validated(env, tmp_path):
    job_id = env.make_job()
    # shrink line 0 to a very short slot before line 1 starts: a slow clip must be cut
    env.client.patch(env.seg_url(job_id, 0), json={"end": 3.0})
    res = env.client.post(env.seg_url(job_id, 0, "/speed"), json={"speed": 0.7})
    assert res.status_code == 200
    assert "too_long" in res.json()["flags"]
    # line 1 starts at 4.5, line 0 at 0.5: the clip (2.9 s / 0.7 = 4.1 s) fits inside 4.0 s window
    # only after the cut at the next line's start
    assert clip_duration(env, job_id, 0, tmp_path) <= 4.0 + 0.05

    for bad in (0.5, 1.6, 0, -1):
        res = env.client.post(env.seg_url(job_id, 1, "/speed"), json={"speed": bad})
        assert res.status_code == 422
        assert isinstance(res.json()["detail"], str)
    assert (
        env.client.post(env.seg_url(job_id, 1, "/speed"), json={"speed": "fast"}).status_code == 422
    )
    assert env.client.post(env.seg_url(job_id, 1, "/speed"), json={}).status_code == 422
    # a line with no raw clip cannot change speed
    assert env.client.post(env.seg_url(job_id, 4, "/speed"), json={"speed": 1.1}).status_code == 409


# ----- audio settings and apply ----------------------------------------------------------


def test_audio_settings_validation_and_storage(env):
    job_id = env.make_job()
    url = f"/api/jobs/{job_id}/audio-settings"
    good = {"voice_volume": 1.2, "background_volume": 0.5, "ducked_volume": 0.1}
    res = env.client.put(url, json=good)
    assert res.status_code == 200 and res.json() == good
    assert env.job(job_id).audio_overrides == good
    assert env.client.get(f"/api/jobs/{job_id}/editor").json()["audio"] == good
    for bad in (
        {**good, "voice_volume": 2.5},
        {**good, "background_volume": -0.1},
        {**good, "ducked_volume": 0.9},  # louder than the background
        {"voice_volume": 1},
    ):
        res = env.client.put(url, json=bad)
        assert res.status_code == 422, bad
        assert isinstance(res.json()["detail"], str)
    assert env.job(job_id).audio_overrides == good


def mixed_ratio(env: Env, job_id: str) -> float:
    """Level of the original music relative to the Vietnamese voice while a line is spoken."""
    mixed = env.job(job_id).artifacts["mixed_audio"]
    a, b = 1.0, 2.5  # inside line 0 (0.5 - 3.0 s), clear of the ducking ramps
    bed = sum(tone_amplitude(mixed, f, a, b) for f in BED_FREQS)
    return bed / tone_amplitude(mixed, VOICE_FREQ, a, b)


def test_apply_rebuilds_without_tts_and_uses_volumes(env):
    job_id = env.make_job()
    calls = len(env.fake.calls)

    # edits: new times for line 0, new text for line 2 (with a stale clip), line 4 has no voice
    assert (
        env.client.patch(env.seg_url(job_id, 0), json={"start": 1.0, "end": 3.0}).status_code == 200
    )
    assert (
        env.client.patch(env.seg_url(job_id, 2), json={"text_vi": "Văn bản đã sửa"}).status_code
        == 200
    )
    res = env.client.post(f"/api/jobs/{job_id}/apply")
    assert res.status_code == 200, res.text
    assert res.json() == {"started": True, "missing_audio": [4]}
    state = env.wait_done(job_id)
    assert state["status"] == "done", state
    assert len(env.fake.calls) == calls, "apply must not call the TTS"

    job = env.job(job_id)
    assert "synthesize" in job.completed_stages  # untouched from the first run
    assert job.segments[4].audio_path is None
    assert all("needs_realign" not in s.flags for s in job.segments)
    srt = Path(job.artifacts["srt"]).read_text(encoding="utf-8")
    assert "Văn bản đã sửa" in srt and "00:00:01,000 --> 00:00:03,000" in srt
    assert Path(job.artifacts["output"]).is_file()
    assert Path(job.artifacts["srt_output"]).read_text(encoding="utf-8") == srt
    voice = job.artifacts["voice_track"]
    assert tone_amplitude(voice, VOICE_FREQ, 1.2, 2.5) > 0.05  # moved to the new start
    assert tone_amplitude(voice, VOICE_FREQ, 0.55, 0.9) < 0.01
    assert tone_amplitude(voice, VOICE_FREQ, 16.7, 18.0) < 0.01  # line 4 stays silent
    dub = env.client.get(f"/api/jobs/{job_id}/media/dub")
    assert dub.status_code == 200 and len(dub.content) > 1000
    assert env.client.get(f"/api/jobs/{job_id}/media/voice").status_code == 200
    assert env.client.get(f"/api/jobs/{job_id}/editor").json()["media"]["dub_video"] is True

    default_ratio = mixed_ratio(env, job_id)

    # quieter original: the editor settings win over the config values in the mix
    settings = {"voice_volume": 1.0, "background_volume": 0.5, "ducked_volume": 0.05}
    assert env.client.put(f"/api/jobs/{job_id}/audio-settings", json=settings).status_code == 200
    assert env.client.post(f"/api/jobs/{job_id}/apply").json()["started"] is True
    assert env.wait_done(job_id)["status"] == "done"
    assert len(env.fake.calls) == calls
    assert mixed_ratio(env, job_id) < default_ratio * 0.6


def test_apply_from_review_does_not_pause_again(env):
    job_id = env.make_job(status=JobStatus.AWAITING_REVIEW)
    res = env.client.post(f"/api/jobs/{job_id}/apply")
    assert res.status_code == 200 and res.json()["missing_audio"] == [4]
    state = env.wait_done(job_id)
    assert state["status"] == "done"
    assert env.job(job_id).review_approved is False  # the gate was simply not part of the run


def test_apply_precondition_errors(env):
    job = Job.create(env.tmp_path / "x.mp4", env.config.paths.work)
    job.status = JobStatus.FAILED
    job.save()
    assert env.client.post(f"/api/jobs/{job.id}/apply").status_code == 409  # no lines yet
    job_id = env.make_job()
    saved = env.job(job_id)
    saved.segments[1].text_vi = ""
    saved.save()
    res = env.client.post(f"/api/jobs/{job_id}/apply")
    assert res.status_code == 422 and "Line 2" in res.json()["detail"]


# ----- media: Range and path safety ------------------------------------------------------


def test_media_range_requests(env):
    job_id = env.make_job()
    url = f"/api/jobs/{job_id}/media/video"
    full = env.client.get(url)
    assert full.status_code == 200
    size = len(full.content)
    assert full.headers["accept-ranges"] == "bytes"

    part = env.client.get(url, headers={"Range": "bytes=10-109"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 10-109/{size}"
    assert part.content == full.content[10:110]

    suffix = env.client.get(url, headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == f"bytes {size - 50}-{size - 1}/{size}"
    assert suffix.content == full.content[-50:]

    tail = env.client.get(url, headers={"Range": f"bytes={size - 5}-"})
    assert tail.status_code == 206 and tail.content == full.content[-5:]

    bad = env.client.get(url, headers={"Range": f"bytes={size + 10}-"})
    assert bad.status_code == 416
    assert bad.headers["content-range"] == f"bytes */{size}"


def test_segment_audio_range_and_no_store(env):
    job_id = env.make_job()
    url = env.seg_url(job_id, 0, "/audio")
    res = env.client.get(url)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["content-type"].startswith("audio/")
    size = len(res.content)
    part = env.client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"] == f"bytes 0-99/{size}"
    assert env.client.get(url, headers={"Range": f"bytes={size}-"}).status_code == 416


def test_waveforms_for_voice_clips_and_background(env):
    job_id = env.make_job(with_audio=(0, 2))
    res = env.client.get(f"/api/jobs/{job_id}/waveforms")
    assert res.status_code == 200
    data = res.json()
    assert sorted(data["voice"]) == ["0", "2"]  # only lines that have a clip
    clip = data["voice"]["0"]
    assert clip["duration"] > 0.3
    assert len(clip["peaks"]) == pytest.approx(clip["duration"] * data["voice_rate"], abs=2)
    assert max(clip["peaks"]) > 20 and all(0 <= v <= 100 for v in clip["peaks"])
    bg = data["background"]
    assert bg["duration"] == pytest.approx(VIDEO_SECONDS, abs=0.5)
    assert len(bg["peaks"]) == pytest.approx(bg["duration"] * data["background_rate"], abs=2)
    assert max(bg["peaks"]) > 20


def test_waveforms_single_line_and_unknown_line(env):
    job_id = env.make_job(with_audio=(0,))
    one = env.client.get(f"/api/jobs/{job_id}/waveforms?index=0").json()
    assert list(one["voice"]) == ["0"] and "background" not in one
    none = env.client.get(f"/api/jobs/{job_id}/waveforms?index=3").json()
    assert none["voice"] == {}
    assert env.client.get(f"/api/jobs/{job_id}/waveforms?index=99").status_code == 404
    assert env.client.get("/api/jobs/aaaaaaaaaaaa/waveforms").status_code == 404


def test_waveform_peaks_helpers(tmp_path):
    import math
    import struct
    import wave

    from video_translator.waveform import WaveformError, file_peaks

    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        quiet = [int(300 * math.sin(i / 5)) for i in range(8000)]
        loud = [int(20000 * math.sin(i / 5)) for i in range(8000)]
        frames = b"".join(struct.pack("<hh", v, v) for v in quiet + loud)
        w.writeframes(frames)
    duration, peaks = file_peaks(wav, 10)
    assert duration == 2.0 and len(peaks) == 20
    assert max(peaks[:10]) < 15 and min(peaks[10:]) > 60
    with pytest.raises(WaveformError):
        file_peaks(tmp_path / "missing.wav", 10)


def test_media_missing_files(env):
    job_id = env.make_job()
    assert env.client.get(f"/api/jobs/{job_id}/media/dub").status_code == 404
    assert env.client.get(f"/api/jobs/{job_id}/media/video").status_code == 200


def test_only_files_inside_job_folders_are_served(env, tmp_path):
    job_id = env.make_job()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    job = env.job(job_id)
    job.segments[0].audio_path = secret
    job.segments[0].aligned_path = secret
    job.artifacts["output"] = secret
    job.artifacts["voice_track"] = job.work_dir / ".." / ".." / "secret.txt"
    job.input_path = secret
    job.save()
    for url in (
        env.seg_url(job_id, 0, "/audio"),
        f"/api/jobs/{job_id}/media/dub",
        f"/api/jobs/{job_id}/media/voice",
        f"/api/jobs/{job_id}/media/video",
    ):
        res = env.client.get(url)
        assert res.status_code == 404, url
        assert "top secret" not in res.text
    data = env.client.get(f"/api/jobs/{job_id}/editor").json()
    assert data["media"] == {"video": False, "dub_video": False, "voice_track": False}
    assert data["segments"][0]["has_audio"] is False


def test_symlink_escape_is_rejected(env, tmp_path):
    job_id = env.make_job()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"outside")
    job = env.job(job_id)
    link = job.work_dir / "linked.wav"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not available")
    job.artifacts["voice_track"] = link
    job.save()
    assert env.client.get(f"/api/jobs/{job_id}/media/voice").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/..%2F..%2Fetc/media/video",
        "/api/jobs/%2e%2e/media/video",
        "/api/jobs/aaaaaaaaaaaag/media/video",
        "/api/jobs/AAAAAAAAAAAA/media/video",
        "/api/jobs/aaaaaaaaaaaa/media/video",
        "/editor/..%2F..%2Fetc",
        "/editor/not-an-id",
    ],
)
def test_bad_ids_and_traversal(env, path):
    assert env.client.get(path).status_code == 404


# ----- page ------------------------------------------------------------------------------


def test_editor_page_route(env):
    job_id = env.make_job()
    res = env.client.get(f"/editor/{job_id}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "<html" in res.text.lower()
    template = (
        Path(__file__).resolve().parents[1] / "src/video_translator/web/templates/editor.html"
    )
    if template.is_file():
        import re

        # the only difference is the ?v=<file version> added to every /static link
        assert re.sub(r"\?v=\d+", "", res.text) == template.read_text(encoding="utf-8")


def test_static_links_are_versioned_and_files_are_not_cached(env):
    import re

    job_id = env.make_job()
    for url in (f"/editor/{job_id}", "/"):
        page = env.client.get(url).text
        links = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
        assert links and all(re.search(r"\?v=\d+$", link) for link in links), links
    link = re.findall(
        r'href="(/static/editor\.css\?v=\d+)"', env.client.get(f"/editor/{job_id}").text
    )
    res = env.client.get(link[0])
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    # a new file version gives a new address, so a browser cannot keep using the old file
    from video_translator.web.assets import add_versions

    static = Path(__file__).resolve().parents[1] / "src/video_translator/web/static"
    before = add_versions('<link href="/static/editor.css">', static)
    assert before.endswith(".css?v=" + before.split("?v=")[1]) and "?v=" in before
    assert (
        add_versions('<link href="/static/nope.css">', static) == '<link href="/static/nope.css">'
    )


def test_editor_routes_are_listed(env):
    paths = {route.path for route in env.client.app.routes}
    assert {
        "/editor/{job_id}",
        "/api/jobs/{job_id}/editor",
        "/api/jobs/{job_id}/segments/{index}",
        "/api/jobs/{job_id}/apply",
        "/api/jobs/{job_id}/media/video",
    } <= paths
    json.dumps(env.client.get("/openapi.json").json())  # the schema still builds
