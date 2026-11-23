"""Translate each Chinese segment into Vietnamese using a translation provider.

Inputs:  job.segments (text_zh)
Outputs: job.segments with text_vi filled in

Resumable: only segments with an empty ``text_vi`` are translated, and every finished batch is
saved to ``<work_dir>/translations/batch_XXX.json`` so a resume never repeats finished work.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job, JobStatus, Segment
from video_translator.providers import registry
from video_translator.providers.gemini_client import QuotaExhaustedError
from video_translator.providers.translation import gemini as _gemini  # noqa: F401 (registers)
from video_translator.providers.translation import ninerouter as _ninerouter  # noqa: F401
from video_translator.providers.translation.base import TranslationProvider
from video_translator.stages.base import PauseRequested, Stage

DEFAULT_BATCH_SIZE = 40
DEFAULT_CONTEXT_SIZE = 3
TRANSLATIONS_DIR = "translations"

ProviderFactory = Callable[[AppConfig], TranslationProvider]


def _default_factory(config: AppConfig) -> TranslationProvider:
    provider_cls = registry.get("translation", config.providers.translation)
    return provider_cls(config=config)


def _batch_file(work_dir: Path, index: int) -> Path:
    return work_dir / TRANSLATIONS_DIR / f"batch_{index:03d}.json"


def _load_batch(path: Path, pending: list[tuple[int, Segment]]) -> list[str] | None:
    """Saved translations for ``pending`` if the file matches them exactly, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["items"]
        if len(items) != len(pending):
            return None
        out: list[str] = []
        for item, (idx, seg) in zip(items, pending, strict=True):
            if item["id"] != idx or item["zh"] != seg.text_zh or not item["vi"].strip():
                return None
            out.append(item["vi"])
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _save_batch(path: Path, pending: list[tuple[int, Segment]], translations: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = [
        {"id": idx, "zh": seg.text_zh, "vi": vi}
        for (idx, seg), vi in zip(pending, translations, strict=True)
    ]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class TranslateStage(Stage):
    name = "translate"

    def __init__(self, provider_factory: ProviderFactory | None = None) -> None:
        self._factory = provider_factory or _default_factory

    def run(self, job: Job, config: AppConfig) -> Job:
        if not job.segments:
            raise ValueError(
                "Nothing to translate: the job has no segments (was the transcription empty?)."
            )
        todo = [i for i, s in enumerate(job.segments) if s.text_zh.strip()]
        if not todo:
            raise ValueError("Nothing to translate: every segment has empty Chinese text.")
        total = len(todo)

        def done_count() -> int:
            return sum(1 for i in todo if job.segments[i].text_vi.strip())

        if done_count() == total:
            return job

        provider = self._factory(config)
        batch_size = int(getattr(provider, "batch_size", DEFAULT_BATCH_SIZE))
        context_size = int(getattr(provider, "context_size", DEFAULT_CONTEXT_SIZE))
        translate_batch = getattr(provider, "translate_batch", None)

        for number, start in enumerate(range(0, len(job.segments), batch_size)):
            pending = [
                (i, job.segments[i])
                for i in range(start, min(start + batch_size, len(job.segments)))
                if job.segments[i].text_zh.strip() and not job.segments[i].text_vi.strip()
            ]
            if not pending:
                continue
            path = _batch_file(job.work_dir, number)
            translations = _load_batch(path, pending)
            if translations is None:
                try:
                    translations = self._translate(
                        provider, translate_batch, job, pending, context_size
                    )
                except QuotaExhaustedError as exc:
                    job.message = (
                        f"Gemini daily quota is used up ({done_count()}/{total} segments "
                        f"translated, progress saved). Run again later (the quota resets daily): "
                        f"video-translator resume {job.work_dir}"
                    )
                    job.save()
                    raise PauseRequested(JobStatus.PAUSED, job.message) from exc
                _save_batch(path, pending, translations)
            for (idx, _seg), vi in zip(pending, translations, strict=True):
                job.segments[idx].text_vi = vi
            job.message = f"Translating {done_count()}/{total}"
            job.save()
        notes = [n for n in getattr(provider, "notes", []) if n]
        if notes:
            job.message = f"Translated {done_count()}/{total} segments ({'; '.join(notes)})"
        return job

    @staticmethod
    def _translate(provider, translate_batch, job: Job, pending, context_size: int) -> list[str]:
        segs = [seg for _i, seg in pending]
        if translate_batch is None:
            result = provider.translate([s.model_copy(deep=True) for s in segs])
            return [s.text_vi for s in result]
        first = pending[0][0]
        context = [s for s in job.segments[max(0, first - context_size) : first] if s.text_vi]
        return translate_batch(segs, context)
