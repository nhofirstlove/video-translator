from __future__ import annotations

from pathlib import Path

import pytest

from video_translator.paths import job_dir, project_root, resolve_path, safe_name


def test_illegal_characters_replaced():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_trailing_dots_and_spaces_stripped():
    assert safe_name("video. . ") == "video"


@pytest.mark.parametrize("name", ["CON", "nul", "COM1", "lpt9", "con.txt"])
def test_reserved_names_changed(name):
    assert safe_name(name) != name
    assert safe_name(name).startswith("_")


def test_chinese_names_kept():
    assert safe_name("我的视频：第一集.mp4") == "我的视频：第一集.mp4"
    assert safe_name("我的视频:第一集?.mp4") == "我的视频_第一集_.mp4"


def test_empty_name():
    assert safe_name("...") == "unnamed"


def test_job_dir_and_resolve(tmp_path):
    assert job_dir(tmp_path, "a/b") == tmp_path / "a_b"
    assert resolve_path(tmp_path) == tmp_path
    assert resolve_path("data/work") == project_root() / "data" / "work"
    assert isinstance(project_root(), Path)
