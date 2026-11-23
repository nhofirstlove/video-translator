"""Tidy-ups on the final Chinese lines: drop filler sounds ("hmm", 嗯, 啊, laughter)."""

from __future__ import annotations

import re

from video_translator.models import Segment

# Interjections and laughter: "hmm", "uh", "ah", "ha ha". Never a subtitle or a voice line.
_FILLER_CHARS = set("嗯啊呃哦噢唉哎哼呵哈唔额诶哇喔嘿咦哟呀呐嘻哧")
_LATIN_FILLER = re.compile(r"^(?:h+m+|m+|u+h+|a+h+|o+h+|e+r+m*)$", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+")


def core(text: str) -> str:
    """The text without punctuation and spaces."""
    return _NON_WORD.sub("", text)


def is_filler(text: str) -> bool:
    """True for text made only of interjections, laughter or "hmm" (and punctuation)."""
    letters = core(text)
    if not letters:
        return True
    if _LATIN_FILLER.match(letters):
        return True
    return len(letters) <= 8 and all(c in _FILLER_CHARS for c in letters)


# Whisper sometimes invents these over music or silence (subtitle credits, "like and subscribe").
_HALLUCINATIONS = (
    "请不吝点赞", "点赞订阅", "订阅转发", "感谢观看", "谢谢观看", "谢谢大家观看",
    "字幕由", "字幕志愿者", "请订阅", "明镜与点点", "优优独播剧场", "中文字幕", "amara.org",
    "字幕由amara.org社区提供",
)  # fmt: skip

# Written for a word that was bleeped out in the original and that nobody could hear: the
# translator guesses it from the context.
BLEEP_MARK = "（哔）"


def is_hallucination(text: str) -> bool:
    """True for a line that is just one of Whisper's known invented phrases."""
    letters = core(text).lower()
    for phrase in _HALLUCINATIONS:
        bare = core(phrase)
        if bare in letters and len(letters) <= len(bare) + 6:
            return True
    return False


def drop_fillers(segments: list[Segment]) -> list[Segment]:
    return [s for s in segments if not is_filler(s.text_zh)]
