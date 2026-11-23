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
from video_translator.stages.align import STRETCH_MAX
from video_translator.stages.base import PauseRequested, Stage

FIT_MARKER = "fit.done"  # written when the "too long?" re-check has run for this job
MIN_CHARS = 8
OVER_LIMIT = STRETCH_MAX  # a line is too long when it needs more than this speed-up to fit
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

        marker = job.work_dir / TRANSLATIONS_DIR / FIT_MARKER
        needs_fit = config.fit.rewrite_rounds > 0 and not marker.exists()
        if done_count() == total and not needs_fit:
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
        fit_note = (
            self._shorten_long_lines(provider, job, config, todo, marker) if needs_fit else ""
        )
        if fit_note:
            notes.append(fit_note)
        if notes:
            job.message = f"Translated {done_count()}/{total} segments ({'; '.join(notes)})"
        return job

    @staticmethod
    def _shorten_long_lines(provider, job: Job, config: AppConfig, todo, marker: Path) -> str:
        """Second step of fitting: ask the translator to rewrite lines that are too long.

        A line is too long when it would need more than a 1.2x speed-up to fit its slot. Best
        effort: a failure only means the lines stay as they are (the stretch in ``align`` still
        does what it can). Returns a short remark for the job message, or "".
        """
        shorten = getattr(provider, "shorten_batch", None)
        if shorten is None:
            return ""
        rate = config.fit.chars_per_second
        batch_size = int(getattr(provider, "batch_size", DEFAULT_BATCH_SIZE))
        rewritten = 0
        failed = ""
        for round_number in range(config.fit.rewrite_rounds):
            target_factor = 1.1 if round_number == 0 else 1.0
            over = [
                i
                for i in todo
                if len(job.segments[i].text_vi.strip())
                > max(MIN_CHARS, round(job.segments[i].duration * rate * OVER_LIMIT))
            ]
            if not over:
                break
            job.message = f"Shortening {len(over)} line(s) that are too long for their time"
            for start in range(0, len(over), batch_size):
                part = over[start : start + batch_size]
                segs = [job.segments[i] for i in part]
                limits = [max(MIN_CHARS, round(s.duration * rate * target_factor)) for s in segs]
                try:
                    shorter = shorten(segs, limits)
                except QuotaExhaustedError:
                    failed = "the Gemini quota ran out"
                    break
                except Exception as exc:
                    failed = str(exc)[:120]
                    break
                for idx, new in zip(part, shorter, strict=True):
                    old = job.segments[idx].text_vi
                    if new.strip() and len(new.strip()) < len(old.strip()):
                        job.segments[idx].text_vi = new.strip()
                        rewritten += 1
                job.save()
            if failed:
                break
        if not failed:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("done", encoding="utf-8")
        still = sum(
            1
            for i in todo
            if len(job.segments[i].text_vi.strip())
            > max(MIN_CHARS, round(job.segments[i].duration * rate * OVER_LIMIT))
        )
        parts = []
        if rewritten:
            parts.append(f"{rewritten} long line(s) were shortened to fit their time")
        if still:
            parts.append(f"{still} line(s) are still long and will be sped up")
        if failed:
            parts.append(f"shortening stopped: {failed}")
        return "; ".join(parts)

    @staticmethod
    def _translate(provider, translate_batch, job: Job, pending, context_size: int) -> list[str]:
        segs = [seg for _i, seg in pending]
        if translate_batch is None:
            result = provider.translate([s.model_copy(deep=True) for s in segs])
            return [s.text_vi for s in result]
        first = pending[0][0]
        context = [s for s in job.segments[max(0, first - context_size) : first] if s.text_vi]
        return translate_batch(segs, context)
