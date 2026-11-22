from __future__ import annotations

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, Segment
from video_translator.stages.subtitles import SubtitlesError, SubtitlesStage
from video_translator.utils.srt import parse_srt


def make_job(tmp_path, segments, stem="video: có dấu?"):
    job = Job(input_path=tmp_path / f"{stem}.mp4", work_dir=tmp_path / "work")
    job.segments = segments
    return job


def test_writes_srt_utf8_without_bom(tmp_path):
    job = make_job(
        tmp_path,
        [
            Segment(start=0, end=2, text_zh="你好", text_vi="Xin chào"),
            Segment(start=3, end=5, text_zh="谢谢", text_vi="Cảm ơn bạn"),
        ],
    )
    out = SubtitlesStage().run(job, AppConfig())
    path = out.artifacts["srt"]
    assert path.parent == job.work_dir / "subtitles"
    assert path.name.endswith(".vi.srt") and ":" not in path.name and "?" not in path.name
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert [c[2] for c in parse_srt(raw.decode("utf-8"))] == ["Xin chào", "Cảm ơn bạn"]


def test_missing_translation_raises(tmp_path):
    job = make_job(tmp_path, [Segment(start=0, end=2, text_zh="你好")])
    with pytest.raises(SubtitlesError, match="translate"):
        SubtitlesStage().run(job, AppConfig())


def test_no_segments_raises(tmp_path):
    with pytest.raises(SubtitlesError):
        SubtitlesStage().run(make_job(tmp_path, []), AppConfig())


def test_srt_disabled_and_burn_in_message(tmp_path):
    cfg = AppConfig.model_validate({"subtitles": {"srt": False, "burn_in": True}})
    job = make_job(tmp_path, [])
    out = SubtitlesStage().run(job, cfg)
    assert "srt" not in out.artifacts and "not supported" in out.message
