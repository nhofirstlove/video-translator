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


def drop_fillers(segments: list[Segment]) -> list[Segment]:
    return [s for s in segments if not is_filler(s.text_zh)]
