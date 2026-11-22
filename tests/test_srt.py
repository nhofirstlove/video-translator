from __future__ import annotations

from video_translator.models import Segment
from video_translator.utils.srt import format_timestamp, parse_srt, segments_to_srt


def seg(start, end, vi):
    return Segment(start=start, end=end, text_zh="x", text_vi=vi)


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3661.5) == "01:01:01,500"
    assert format_timestamp(59.9996) == "00:01:00,000"
    assert format_timestamp(0.0004) == "00:00:00,000"
    assert format_timestamp(-3) == "00:00:00,000"
    assert format_timestamp(3599.9999) == "01:00:00,000"


def test_basic_roundtrip_with_diacritics():
    text = "Xin chào các bạn, hôm nay chúng ta học tiếng Việt."
    out = segments_to_srt([seg(1.0, 4.0, text), seg(5.0, 7.5, "Đây là ví dụ thứ hai")])
    assert out.startswith("1\n00:00:01,000 --> 00:00:04,000\n")
    assert "\r" not in out and out.endswith("\n")
    cues = parse_srt(out)
    assert len(cues) == 2
    assert cues[0][2].replace("\n", " ") == text
    assert cues[1] == (5.0, 7.5, "Đây là ví dụ thứ hai")


def test_wrapping_into_two_balanced_lines():
    text = "một hai ba bốn năm sáu bảy tám chín mười mười một mười hai mười ba"
    out = segments_to_srt([seg(0, 6, text)], max_chars_per_line=42)
    cues = parse_srt(out)
    assert len(cues) == 1
    lines = cues[0][2].split("\n")
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == text


def test_long_text_split_into_cues_proportionally():
    text = " ".join(["từ"] * 60)  # 179 chars, needs more than 2 lines of 42
    cues = parse_srt(segments_to_srt([seg(10, 20, text)], max_chars_per_line=42, max_lines=2))
    assert len(cues) >= 3
    assert cues[0][0] == 10.0
    assert 19.99 < cues[-1][1] < 21  # last cue may be extended for reading speed
    for (_, end, _), (start, _, _) in zip(cues, cues[1:], strict=False):
        assert end <= start + 1e-9
    for _, _, body in cues:
        assert len(body.split("\n")) <= 2
        assert all(len(line) <= 42 for line in body.split("\n"))
    assert " ".join(c[2].replace("\n", " ") for c in cues) == text


def test_overlap_trimmed_numbering_and_empty_skipped():
    out = segments_to_srt([seg(0, 5, "một"), seg(2, 4, "  "), seg(3, 6, "hai")])
    cues = parse_srt(out)
    assert [c[2] for c in cues] == ["một", "hai"]
    assert cues[0][1] <= cues[1][0]
    assert out.split("\n")[0] == "1" and "\n2\n" in out


def test_min_duration_extended_when_free():
    cues = parse_srt(segments_to_srt([seg(0, 0.2, "a"), seg(5, 5.1, "b")], min_duration=1.0))
    assert cues[0][1] == 1.0
    assert abs(cues[1][1] - 6.0) < 1e-9


def test_min_duration_limited_by_next_cue():
    cues = parse_srt(segments_to_srt([seg(0, 0.2, "a"), seg(0.5, 3, "b")]))
    assert cues[0][1] == 0.5


def test_cps_extends_end():
    text = "x" * 40
    cues = parse_srt(segments_to_srt([seg(0, 1.0, text)], max_chars_per_line=42, max_cps=20))
    assert abs(cues[0][1] - 2.0) < 1e-9


def test_same_start_still_valid():
    cues = parse_srt(segments_to_srt([seg(1, 3, "a"), seg(1, 3, "b")]))
    assert len(cues) == 2 and cues[0][1] <= cues[1][0] and cues[0][1] > cues[0][0]


def test_empty_input_and_bom_parse():
    assert segments_to_srt([]) == ""
    assert parse_srt("﻿1\r\n00:00:01,000 --> 00:00:02,500\r\nHi\r\n\r\n") == [(1.0, 2.5, "Hi")]
