from __future__ import annotations

import os

import pytest

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.review_file import (
    ReviewFileError,
    apply_review,
    read_review_file,
    review_path,
    write_review_file,
)
from video_translator.stages.base import PauseRequested
from video_translator.stages.review import ReviewStage


def make_job(tmp_path):
    segments = [
        Segment(start=62.5, end=65.1, text_zh="你好，世界", text_vi="Xin chào thế giới"),
        Segment(start=66.0, end=70.0, text_zh="今天天气很好", text_vi="Hôm nay trời đẹp lắm"),
        Segment(start=71.0, end=72.0, text_zh="再见", text_vi="Tạm biệt"),
    ]
    return Job(input_path=tmp_path / "v.mp4", work_dir=tmp_path, segments=segments)


def test_write_format_and_round_trip(tmp_path):
    job = make_job(tmp_path)
    path = write_review_file(job)
    assert path == tmp_path / "review" / "review.txt"
    text = path.read_text(encoding="utf-8")
    assert "#0  00:01:02,500 --> 00:01:05,100" in text
    assert "ZH: 你好，世界" in text and "VI: Hôm nay trời đẹp lắm" in text
    assert read_review_file(path) == {
        0: "Xin chào thế giới",
        1: "Hôm nay trời đẹp lắm",
        2: "Tạm biệt",
    }


def test_crlf_bom_and_wrapped_vi(tmp_path):
    job = make_job(tmp_path)
    path = write_review_file(job)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = text.replace("VI: Tạm biệt", "VI: Tạm biệt nhé\n   các bạn thân mến")
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    result = read_review_file(path, expected_ids=[0, 1, 2])
    assert result[2] == "Tạm biệt nhé các bạn thân mến"
    apply_review(job)
    assert job.segments[2].text_vi == "Tạm biệt nhé các bạn thân mến"


def edit(path, old, new):
    data = path.read_bytes().decode("utf-8")
    assert old in data
    path.write_bytes(data.replace(old, new).encode("utf-8"))


def test_validation_errors(tmp_path):
    job = make_job(tmp_path)
    path = write_review_file(job)
    original = path.read_bytes()

    edit(path, "#1  ", "#7  ")
    with pytest.raises(ReviewFileError, match="unknown segment #7"):
        apply_review(job)

    path.write_bytes(original)
    edit(path, "#1  ", "#0  ")
    with pytest.raises(ReviewFileError, match="appears twice"):
        read_review_file(path)

    path.write_bytes(original)
    edit(path, "VI: Hôm nay trời đẹp lắm", "VI: ")
    with pytest.raises(ReviewFileError, match=r"Line \d+: the 'VI:' line of segment #1 is empty"):
        apply_review(job)

    path.write_bytes(original)
    edit(path, "VI: Hôm nay trời đẹp lắm", "")
    with pytest.raises(ReviewFileError, match="no 'VI:' line"):
        read_review_file(path)

    path.write_bytes(original)
    edit(path, "ZH: 再见", "ZH: 别的")
    with pytest.raises(ReviewFileError, match="does not match"):
        apply_review(job)

    path.write_bytes(original)
    edit(path, "VI: Tạm biệt", "VI: Tạm biệt\r\n\r\nlạc lõng")
    with pytest.raises(ReviewFileError, match="unexpected text"):
        read_review_file(path)

    path.write_bytes(original)
    text = original.decode("utf-8")
    cut = text.index("#2  ")
    path.write_bytes(text[:cut].encode("utf-8"))
    with pytest.raises(ReviewFileError, match="missing"):
        read_review_file(path, expected_ids=[0, 1, 2])


def test_stage_pause_then_approve_with_edits(tmp_path):
    job = make_job(tmp_path)
    stage = ReviewStage()
    with pytest.raises(PauseRequested) as info:
        stage.run(job, AppConfig())
    assert info.value.status == JobStatus.AWAITING_REVIEW
    path = review_path(job)
    assert str(path) in info.value.message
    assert f"video-translator resume {tmp_path} --approve" in info.value.message

    # second pause must not rewrite the user's edits
    edit(path, "VI: Tạm biệt", "VI: Hẹn gặp lại")
    with pytest.raises(PauseRequested):
        stage.run(job, AppConfig())
    assert "Hẹn gặp lại" in path.read_text(encoding="utf-8")

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    job.review_approved = True
    stage.run(job, AppConfig())
    assert job.segments[2].text_vi == "Hẹn gặp lại"
    assert job.segments[0].text_vi == "Xin chào thế giới"


def test_stage_approved_unchanged_file_keeps_segments(tmp_path):
    job = make_job(tmp_path)
    with pytest.raises(PauseRequested):
        ReviewStage().run(job, AppConfig())
    job.segments[0].text_vi = "Sửa từ giao diện web"  # edited through the web UI
    job.review_approved = True
    ReviewStage().run(job, AppConfig())
    assert job.segments[0].text_vi == "Sửa từ giao diện web"


def test_stage_review_disabled_passthrough(tmp_path):
    job = make_job(tmp_path)
    out = ReviewStage().run(job, AppConfig(review=False))
    assert out is job
    assert not review_path(job).exists()
