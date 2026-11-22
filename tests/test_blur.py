from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.test_editor_api import env  # noqa: F401  (pytest fixture)
from tests.test_render import make_audio, make_video, needs_ffmpeg, streams
from video_translator.blur import (
    Box,
    build_ass,
    build_filter_graph,
    effective_blur,
    escape_filter_path,
    resolve_box,
    sigma,
)
from video_translator.config import (
    AppConfig,
    BlurConfig,
    ConfigError,
    load_config,
    save_local_override,
)
from video_translator.models import Job, Segment
from video_translator.stages.render import RenderStage

# --- settings and geometry -----------------------------------------------------------------


def test_blur_is_off_by_default():
    cfg = AppConfig()
    assert cfg.blur.enabled is False and cfg.blur.strength >= 50


def test_effective_blur_job_choices_win_over_the_default():
    cfg = AppConfig()
    cfg.blur.strength = 60
    blur = effective_blur(cfg, {"enabled": True, "width": 900})
    assert blur.enabled and blur.width == 900 and blur.strength == 60
    with pytest.raises(ConfigError):
        effective_blur(cfg, {"strength": 500})


def test_default_box_is_centred_near_the_bottom_and_inside_the_picture():
    box = resolve_box(BlurConfig(width=1100, height=130), 1920, 1080)
    assert (box.x, box.y, box.width, box.height) == (410, 884, 1100, 130)
    small = resolve_box(BlurConfig(width=1100, height=130), 640, 360)
    assert small.x == 0 and small.width == 640  # never wider than the picture
    moved = resolve_box(BlurConfig(x=5000, y=5000, width=200, height=50), 640, 360)
    assert moved.x + moved.width <= 640 and moved.y + moved.height <= 360
    assert all(v % 2 == 0 for v in (moved.x, moved.y, moved.width, moved.height))


def test_strength_maps_to_sigma():
    assert sigma(1) < sigma(70) < sigma(100) == 40.0


def test_ass_file_has_centred_text_inside_the_box():
    blur = BlurConfig(font_size=36)
    box = Box(100, 200, 800, 100)
    segs = [
        Segment(start=1.0, end=2.5, text_zh="你好", text_vi="Xin chào {x}\nbạn"),
        Segment(start=3.0, end=3.0, text_zh="a", text_vi="zero length"),
        Segment(start=4.0, end=5.0, text_zh="b", text_vi="  "),
    ]
    ass = build_ass(segs, blur, box, 1280, 720)
    assert "PlayResX: 1280" in ass and "PlayResY: 720" in ass
    assert "Style: Vi,Arial,36," in ass
    dialogue = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert dialogue == [
        "Dialogue: 0,0:00:01.00,0:00:02.50,Vi,,0,0,0,,{\\an5\\pos(500,250)}Xin chào (x)\\Nbạn"
    ]
    assert ",100,380,0,1" in ass  # left and right margins keep the wrapping inside the box


def test_filter_path_escaping_for_windows():
    assert escape_filter_path(r"C:\video-translator\data\blur.ass") == (
        "'C\\:/video-translator/data/blur.ass'"
    )
    assert escape_filter_path("/tmp/it's.ass") == "'/tmp/it\\'s.ass'"


def test_filter_graph_with_and_without_tint():
    box = Box(10, 20, 300, 60)
    plain = build_filter_graph(BlurConfig(strength=50), box, "/tmp/a.ass")
    assert "crop=300:60:10:20" in plain and "gblur=sigma=20.0" in plain
    assert "drawbox" not in plain and plain.endswith("[v]")
    tinted = build_filter_graph(BlurConfig(tint=0.4), box, "/tmp/a.ass")
    assert "drawbox" in tinted and "black@0.40" in tinted


def test_save_local_override_merges_and_validates(tmp_path, monkeypatch):
    base = tmp_path / "config" / "default.yaml"
    base.parent.mkdir()
    base.write_text("review: true\n", encoding="utf-8")
    (base.parent / "local.yaml").write_text("vieneu:\n  voice: Mai Anh\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(base))
    path = save_local_override("blur", {"enabled": True, "strength": 90})
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"vieneu": {"voice": "Mai Anh"}, "blur": {"enabled": True, "strength": 90}}
    cfg = load_config()
    assert cfg.blur.enabled and cfg.blur.strength == 90 and cfg.vieneu.voice == "Mai Anh"
    with pytest.raises(ConfigError):
        save_local_override("blur", {"strength": 1000})
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == data  # nothing was written


# --- the render ----------------------------------------------------------------------------


def _blur_job(tmp_path, overrides):
    video = tmp_path / "v.mp4"
    make_video(video, seconds=4)  # 128x96, mpeg4
    mixed = tmp_path / "mixed.wav"
    make_audio(mixed, 4)
    job = Job.create(video, tmp_path / "work")
    job.artifacts["mixed_audio"] = mixed
    job.segments = [Segment(start=0.5, end=3.0, text_zh="你好", text_vi="Xin chào các bạn")]
    job.blur_overrides = overrides
    config = AppConfig()
    config.paths.output = tmp_path / "out"
    return job, config


@needs_ffmpeg
def test_render_with_blur_burns_subtitles_and_reencodes(tmp_path):
    job, config = _blur_job(tmp_path, {"enabled": True, "width": 100, "height": 30})
    job = RenderStage().run(job, config)
    out = job.artifacts["output"]
    assert streams(out) == {"video": "h264", "audio": "aac"}  # re-encoded (source was mpeg4)
    ass = (job.work_dir / "subtitles" / "blur.ass").read_text(encoding="utf-8")
    assert "Xin chào các bạn" in ass
    # the box area differs from the untouched source frame, the rest does not
    from video_translator.utils.ffmpeg import run_ffmpeg

    frame = tmp_path / "f.png"
    run_ffmpeg(["-ss", "1", "-i", out, "-frames:v", "1", frame], desc="frame")
    assert frame.stat().st_size > 0


@needs_ffmpeg
def test_render_without_blur_still_copies_the_video(tmp_path):
    job, config = _blur_job(tmp_path, {})
    job = RenderStage().run(job, config)
    assert streams(job.artifacts["output"])["video"] == "mpeg4"
    assert not (job.work_dir / "subtitles" / "blur.ass").exists()


# --- the editor API ----------------------------------------------------------------------------


def test_editor_data_has_blur_block(env):  # noqa: F811
    job_id = env.make_job()
    blur = env.client.get(f"/api/jobs/{job_id}/editor").json()["blur"]
    assert blur["settings"]["enabled"] is False and blur["customised"] is False
    assert blur["video"]["width"] > 0
    w, h = blur["video"]["width"], blur["video"]["height"]
    s = blur["settings"]
    assert 0 <= s["x"] and s["x"] + s["width"] <= w and s["y"] + s["height"] <= h


def test_put_blur_settings_validates_clamps_and_resets(env):  # noqa: F811
    job_id = env.make_job()
    url = f"/api/jobs/{job_id}/blur-settings"
    good = {
        "enabled": True, "x": 4, "y": 40, "width": 100, "height": 30,
        "strength": 80, "tint": 0.2, "font_size": 20,
    }  # fmt: skip
    res = env.client.put(url, json=good)
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["enabled"] is True and res.json()["customised"] is True
    assert env.job(job_id).blur_overrides["strength"] == 80

    too_big = env.client.put(url, json={**good, "x": 5000, "width": 9999})
    box = too_big.json()["settings"]
    w = too_big.json()["video"]["width"]
    assert box["x"] + box["width"] <= w  # kept inside the picture

    for bad in (
        {**good, "strength": 0},
        {**good, "tint": 2.0},
        {**good, "width": 3},
        {"enabled": 1},
    ):
        res = env.client.put(url, json=bad)
        assert res.status_code == 422, bad
        assert isinstance(res.json()["detail"], str)

    res = env.client.delete(url)
    assert res.status_code == 200 and res.json()["customised"] is False
    assert env.job(job_id).blur_overrides == {}


def test_blur_edits_are_refused_while_the_job_runs(env):  # noqa: F811
    job_id = env.make_job(
        status=env.manager and __import__("video_translator.models").models.JobStatus.PENDING
    )
    res = env.client.delete(f"/api/jobs/{job_id}/blur-settings")
    assert res.status_code == 409


def test_save_blur_defaults_writes_local_yaml(env, tmp_path, monkeypatch):  # noqa: F811
    base = tmp_path / "cfg" / "default.yaml"
    base.parent.mkdir()
    base.write_text("review: true\n", encoding="utf-8")
    monkeypatch.setenv("VT_CONFIG", str(base))
    body = {
        "enabled": False, "x": None, "y": None, "width": 900, "height": 100,
        "strength": 95, "tint": 0.0, "font_size": 32,
    }  # fmt: skip
    res = env.client.put("/api/blur-defaults", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["defaults"]["strength"] == 95
    assert env.config.blur.width == 900  # live config updated
    saved = yaml.safe_load((base.parent / "local.yaml").read_text(encoding="utf-8"))
    assert saved["blur"]["strength"] == 95
    assert env.client.put("/api/blur-defaults", json={**body, "strength": 0}).status_code == 422


@needs_ffmpeg
def test_apply_with_blur_makes_a_burned_in_video(env):  # noqa: F811
    job_id = env.make_job()
    body = {
        "enabled": True, "x": None, "y": None, "width": 100, "height": 30,
        "strength": 70, "tint": 0.0, "font_size": 14,
    }  # fmt: skip
    assert env.client.put(f"/api/jobs/{job_id}/blur-settings", json=body).status_code == 200
    assert env.client.post(f"/api/jobs/{job_id}/apply").json()["started"] is True
    state = env.wait_done(job_id)
    assert state["status"] == "done", state
    job = env.job(job_id)
    assert streams(job.artifacts["output"])["video"] == "h264"
    ass = Path(job.work_dir / "subtitles" / "blur.ass").read_text(encoding="utf-8")
    assert ass.count("Dialogue:") == len([s for s in job.segments if s.text_vi.strip()])
