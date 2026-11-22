"""Human-editable review file (plain UTF-8 text that works in Windows Notepad).

Location: ``<work_dir>/review/review.txt``. Each segment is a block::

    #12  00:01:02,500 --> 00:01:05,100
    ZH: 中文原文
    VI: Bản dịch tiếng Việt (the only line you edit; may wrap onto the next lines)

    (blank line ends the block)

Lines starting with ``##`` before the first block are instructions and are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from video_translator.models import Job

REVIEW_DIR = "review"
REVIEW_FILE_NAME = "review.txt"
WRITTEN_MARKER_NAME = ".written"

_HEADER = re.compile(r"^#(\d+)\s+(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})\s*$")

HEADER_TEXT = """\
## REVIEW THE VIETNAMESE TRANSLATION
## Edit ONLY the lines that start with "VI:". A VI text may continue on the next lines,
## but it must not contain an empty line (an empty line ends the segment).
## Do NOT change the "#number  time --> time" lines or the "ZH:" lines.
## Do not delete a segment and do not leave a "VI:" empty.
## Save the file (keep UTF-8 encoding), then run:  video-translator resume <work folder> --approve
##
"""


class ReviewFileError(ValueError):
    """The review file is malformed or inconsistent with the job."""


@dataclass
class ReviewBlock:
    index: int
    line: int  # 1-based line number of the header
    zh: str
    vi: str
    vi_line: int  # 1-based line number of the VI: line (0 if missing)


def review_dir(job: Job) -> Path:
    return job.work_dir / REVIEW_DIR


def review_path(job: Job) -> Path:
    return review_dir(job) / REVIEW_FILE_NAME


def written_marker_path(job: Job) -> Path:
    return review_dir(job) / WRITTEN_MARKER_NAME


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _reviewable(job: Job) -> list[int]:
    return [i for i, seg in enumerate(job.segments) if seg.text_zh.strip()]


def write_review_file(job: Job) -> Path:
    """Write ``review/review.txt`` (always overwrites) and record when we wrote it."""
    path = review_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER_TEXT.rstrip("\n"), ""]
    for i in _reviewable(job):
        seg = job.segments[i]
        lines.append(f"#{i}  {format_timestamp(seg.start)} --> {format_timestamp(seg.end)}")
        lines.append(f"ZH: {_one_line(seg.text_zh)}")
        lines.append(f"VI: {_one_line(seg.text_vi)}")
        lines.append("")
    # CRLF so old Notepad versions show proper lines too.
    path.write_bytes("\r\n".join(lines).encode("utf-8"))
    written_marker_path(job).write_text(str(path.stat().st_mtime_ns), encoding="utf-8")
    return path


def _parse(path: Path) -> list[ReviewBlock]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReviewFileError(f"Cannot read review file {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReviewFileError(
            f"{path} is not valid UTF-8. Save it again as UTF-8 (Notepad: Save As > Encoding)."
        ) from exc

    blocks: list[ReviewBlock] = []
    seen: set[int] = set()
    current: ReviewBlock | None = None
    in_vi = False  # still collecting VI continuation lines
    for lineno, line in enumerate(text.splitlines(), start=1):
        header = _HEADER.match(line.strip())
        if header:
            index = int(header.group(1))
            if index in seen:
                raise ReviewFileError(f"Line {lineno}: segment #{index} appears twice.")
            seen.add(index)
            current = ReviewBlock(index=index, line=lineno, zh="", vi="", vi_line=0)
            blocks.append(current)
            in_vi = False
            continue
        stripped = line.strip()
        if current is None:
            if stripped and not stripped.startswith("##"):
                raise ReviewFileError(
                    f"Line {lineno}: text before the first segment header: {stripped[:40]!r}. "
                    "Do not change the '#number  time --> time' lines."
                )
            continue
        if not stripped:
            in_vi = False
            continue
        if stripped.startswith("##"):
            continue
        if stripped.startswith("#") and not in_vi and current.vi_line == 0:
            raise ReviewFileError(
                f"Line {lineno}: malformed segment header {stripped[:50]!r}. "
                "Expected '#number  00:00:00,000 --> 00:00:00,000'."
            )
        if line.startswith("ZH:") and not current.zh and current.vi_line == 0:
            current.zh = line[3:].strip()
            in_vi = False
        elif line.startswith("VI:") and current.vi_line == 0:
            current.vi = line[3:].strip()
            current.vi_line = lineno
            in_vi = True
        elif in_vi:
            current.vi = f"{current.vi} {stripped}".strip()
        else:
            raise ReviewFileError(
                f"Line {lineno}: unexpected text in segment #{current.index}: "
                f"{stripped[:40]!r}. Use a single 'VI:' line (no empty lines inside it)."
            )
    for block in blocks:
        if block.vi_line == 0:
            raise ReviewFileError(f"Line {block.line}: segment #{block.index} has no 'VI:' line.")
    return blocks


def read_review_file(path: str | Path, expected_ids: list[int] | None = None) -> dict[int, str]:
    """Read the edited Vietnamese lines as ``{segment index: text}``.

    Accepts Windows line endings and a UTF-8 BOM. If ``expected_ids`` is given, every one of
    them must be present and no other id may appear.
    """
    blocks = _parse(Path(path))
    result = {b.index: b.vi for b in blocks}
    if expected_ids is not None:
        expected = set(expected_ids)
        for block in blocks:
            if block.index not in expected:
                raise ReviewFileError(
                    f"Line {block.line}: unknown segment #{block.index}. "
                    "The '#number' was probably changed by mistake."
                )
        missing = sorted(expected - set(result))
        if missing:
            shown = ", ".join(f"#{i}" for i in missing[:10])
            raise ReviewFileError(
                f"Segment(s) missing from the review file: {shown}. Do not delete segments."
            )
    return result


def apply_review(job: Job, path: str | Path | None = None) -> Job:
    """Copy the edited VI lines from the review file into ``job.segments[i].text_vi``."""
    path = Path(path) if path else review_path(job)
    expected = _reviewable(job)
    blocks = _parse(path)
    read_review_file(path, expected_ids=expected)  # id validation (reuses the same rules)
    for block in blocks:
        seg = job.segments[block.index]
        if _one_line(block.zh) != _one_line(seg.text_zh):
            raise ReviewFileError(
                f"Line {block.line}: the ZH text of segment #{block.index} does not match the "
                "original. Do not change the '#number' or 'ZH:' lines."
            )
        if not block.vi.strip():
            raise ReviewFileError(
                f"Line {block.vi_line}: the 'VI:' line of segment #{block.index} is empty."
            )
    for block in blocks:
        job.segments[block.index].text_vi = block.vi.strip()
    return job


def file_edited_since_written(job: Job) -> bool:
    """True if the review file exists and was modified after we wrote it."""
    path = review_path(job)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return False
    try:
        written = int(written_marker_path(job).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True  # no record: treat the file as the user's
    return mtime > written


__all__ = [
    "ReviewFileError",
    "apply_review",
    "file_edited_since_written",
    "format_timestamp",
    "read_review_file",
    "review_path",
    "write_review_file",
]
