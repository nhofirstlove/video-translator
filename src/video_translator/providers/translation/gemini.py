"""Gemini translation provider: Chinese -> natural spoken Vietnamese for dubbing."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from video_translator.models import Segment
from video_translator.providers.gemini_client import GeminiClient
from video_translator.providers.registry import register
from video_translator.providers.translation.base import TranslationProvider

if TYPE_CHECKING:
    from video_translator.config import AppConfig

BATCH_SIZE = 40
CONTEXT_SIZE = 3
VI_CHARS_PER_SECOND = 15  # comfortable spoken Vietnamese rate, used for the length hint
MIN_MAX_CHARS = 8

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

PROMPT_TEMPLATE = """\
You translate Chinese speech into Vietnamese for dubbing a video.

Rules:
- Write natural, spoken Vietnamese suitable for a narrator/speaker, not a literal translation.
- Keep names (people, places, brands) consistent across segments and with the context.
- Keep numbers, dates and units correct.
- Do NOT merge or split segments: return exactly one output per input id.
- Each segment has "seconds" (the time slot) and "max_chars" (target maximum length of the
  Vietnamese text, about {rate} characters per second). When the slot is short, be concise.
- "context" (if present) holds the previous segments with their Vietnamese translation. It is
  read-only: use it for consistency, do NOT translate or return it.
- A segment whose "zh" is （哔） is a word that was bleeped out in the original and could not be
  heard. Replace it with the word that most likely was there, judging from the sentence around it
  (a natural Vietnamese word of the same register, mild if unsure). Never write "bleep".

Input JSON:
{payload}

Reply with JSON only, in exactly this shape:
{{"segments": [{{"id": 0, "vi": "..."}}]}}
"""


class TranslationError(RuntimeError):
    """The model's answer could not be turned into one translation per segment."""


def max_chars_for(seconds: float) -> int:
    """Target maximum Vietnamese length for a segment lasting ``seconds``."""
    return max(MIN_MAX_CHARS, round(max(seconds, 0.0) * VI_CHARS_PER_SECOND))


def parse_response(text: str, expected_ids: Sequence[int]) -> dict[int, str]:
    """Parse the model's JSON answer into ``{id: vi}``; raise TranslationError if unusable.

    Tolerates markdown code fences, a bare list instead of ``{"segments": [...]}`` and extra
    ids. Every expected id must be present with non-empty text.
    """
    cleaned = text.strip().lstrip("﻿")
    fence = _FENCE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"the answer is not valid JSON ({exc.msg})") from exc
    items = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise TranslationError('the answer has no "segments" list')
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            seg_id = int(item.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        vi = item.get("vi")
        if isinstance(vi, str) and vi.strip():
            result[seg_id] = vi.strip()
    missing = [i for i in expected_ids if i not in result]
    if missing:
        raise TranslationError(f"missing translations for ids {missing[:10]}")
    return {i: result[i] for i in expected_ids}


@register("translation", "gemini")
class GeminiTranslation(TranslationProvider):
    """Translates in batches, passing the previous translated segments as read-only context."""

    def __init__(
        self,
        client: GeminiClient | None = None,
        config: AppConfig | None = None,
        batch_size: int = BATCH_SIZE,
        context_size: int = CONTEXT_SIZE,
        **options,
    ) -> None:
        super().__init__(**options)
        self._client = client
        self._config = config
        self.batch_size = batch_size
        self.context_size = context_size

    @property
    def client(self) -> GeminiClient:
        if self._client is None:
            config = self._config
            if config is None:
                from video_translator.config import load_config

                config = load_config()
            self._client = GeminiClient(
                model=config.gemini.model,
                requests_per_minute=config.gemini.requests_per_minute,
            )
        return self._client

    def translate(self, segments: list[Segment]) -> list[Segment]:
        """Return copies of ``segments`` (same order) with ``text_vi`` filled in."""
        result = [seg.model_copy(deep=True) for seg in segments]
        for start in range(0, len(result), self.batch_size):
            batch = result[start : start + self.batch_size]
            context = result[max(0, start - self.context_size) : start]
            translations = self.translate_batch(batch, context)
            for seg, vi in zip(batch, translations, strict=True):
                seg.text_vi = vi
        return result

    def translate_batch(
        self, batch: Sequence[Segment], context: Sequence[Segment] = ()
    ) -> list[str]:
        """Translate one batch; returns one Vietnamese string per segment, in order.

        ``context`` are earlier segments (with ``text_vi``) shown to the model read-only.
        Asks again once if the answer is unusable, then raises TranslationError.
        """
        if not batch:
            return []
        prompt = self._build_prompt(batch, context)
        ids = list(range(len(batch)))
        last_error = ""
        for _attempt in range(2):
            text = self.client.generate_text(prompt, json_mode=True)
            try:
                parsed = parse_response(text, ids)
            except TranslationError as exc:
                last_error = str(exc)
                continue
            return [parsed[i] for i in ids]
        raise TranslationError(
            f"Gemini did not return a usable translation for a batch of {len(batch)} segments "
            f"after 2 attempts: {last_error}."
        )

    def _build_prompt(self, batch: Sequence[Segment], context: Sequence[Segment]) -> str:
        payload: dict[str, object] = {
            "segments": [
                {
                    "id": i,
                    "zh": seg.text_zh,
                    "seconds": round(seg.duration, 2),
                    "max_chars": max_chars_for(seg.duration),
                }
                for i, seg in enumerate(batch)
            ]
        }
        ctx = [
            {"zh": seg.text_zh, "vi": seg.text_vi}
            for seg in context[-self.context_size :]
            if seg.text_vi
        ]
        if ctx:
            payload["context"] = ctx
        return PROMPT_TEMPLATE.format(
            rate=VI_CHARS_PER_SECOND, payload=json.dumps(payload, ensure_ascii=False, indent=1)
        )
