"""Transcribe Chinese speech into timed segments using an ASR provider.

Inputs:  job.chunks (audio files that overlap by a few seconds)
Outputs: job.segments with start, end, text_zh (video-absolute times, sorted, de-duplicated)

Each chunk's result is saved to ``<work_dir>/transcripts/chunk_XXX.json`` as soon as it is
done, so a resumed job (or one paused by the Gemini daily quota) never repeats finished work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job, JobStatus, Segment
from video_translator.providers import registry
from video_translator.providers.asr import gemini as _gemini  # noqa: F401  (registers "gemini")
from video_translator.providers.asr import whisper_gemini as _wg  # noqa: F401  (registers it)
from video_translator.providers.asr import whisper_only as _wo  # noqa: F401  (registers "whisper")
from video_translator.providers.asr.base import ASRProvider
from video_translator.providers.asr.whisper_timing import WhisperError
from video_translator.providers.gemini_client import QuotaExhaustedError
from video_translator.stages.base import PauseRequested, Stage

EDGE_SECONDS = 2.0  # a segment this close to a chunk border may be cut off
OVERLAP_RATIO = 0.5
SIMILARITY = 0.6
_TIME_TOLERANCE = 0.01


def _transcript_path(job: Job, chunk: Chunk) -> Path:
    return job.work_dir / "transcripts" / f"chunk_{chunk.index:03d}.json"


def _load_cached(path: Path, chunk: Chunk, tag: str = "gemini") -> list[Segment] | None:
    """Segments (relative to the chunk) saved earlier, or None if absent/stale/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            data.get("provider", "gemini") != tag
            or data.get("index") != chunk.index
            or abs(float(data["start"]) - chunk.start) > _TIME_TOLERANCE
            or abs(float(data["end"]) - chunk.end) > _TIME_TOLERANCE
        ):
            return None
        return [Segment(**item) for item in data["segments"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_cached(path: Path, chunk: Chunk, segments: list[Segment], tag: str = "gemini") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "provider": tag,
        "index": chunk.index,
        "start": chunk.start,
        "end": chunk.end,
        "segments": [{"start": s.start, "end": s.end, "text_zh": s.text_zh} for s in segments],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class _Item:
    seg: Segment
    near_edge: bool


def _overlap(a: Segment, b: Segment) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _same_text(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY


def _is_duplicate(a: Segment, b: Segment) -> bool:
    shorter = min(a.duration, b.duration)
    if shorter <= 0 or _overlap(a, b) / shorter <= OVERLAP_RATIO:
        return False
    return _same_text(a.text_zh, b.text_zh)


def merge_chunk_segments(per_chunk: list[tuple[Chunk, list[Segment]]]) -> list[Segment]:
    """Merge video-absolute segments of overlapping chunks into one sorted list.

    A segment of a later chunk is dropped when it overlaps in time (> 50 % of the shorter one)
    with an already kept segment and has near-identical or contained text. Of two duplicates the
    one NOT near a chunk border wins (borders are where speech gets cut off).
    """
    kept: list[_Item] = []
    ordered = sorted(per_chunk, key=lambda cs: cs[0].start)
    for position, (chunk, segments) in enumerate(ordered):
        has_prev = position > 0
        has_next = position < len(ordered) - 1
        for seg in segments:
            near_edge = (has_prev and seg.start - chunk.start < EDGE_SECONDS) or (
                has_next and chunk.end - seg.end < EDGE_SECONDS
            )
            item = _Item(seg, near_edge)
            duplicate_of = next((k for k in kept if _is_duplicate(k.seg, seg)), None)
            if duplicate_of is None:
                kept.append(item)
            elif duplicate_of.near_edge and not near_edge:
                duplicate_of.seg, duplicate_of.near_edge = seg, False
    result = sorted((k.seg for k in kept), key=lambda s: (s.start, s.end))
    for prev, cur in zip(result, result[1:], strict=False):
        if prev.end > cur.start:
            if cur.start - prev.start >= 0.2:
                prev.end = cur.start
            else:
                cur.start = prev.end
                cur.end = max(cur.end, cur.start + 0.2)
    return result


class TranscribeStage(Stage):
    name = "transcribe"

    def __init__(self, provider: ASRProvider | None = None) -> None:
        """``provider`` is for tests; normally it is built from ``config.providers.asr``."""
        self._provider = provider

    def _get_provider(self, config: AppConfig) -> ASRProvider:
        if self._provider is not None:
            return self._provider
        cls = registry.get("asr", config.providers.asr)
        return cls(
            model=config.gemini.model,
            requests_per_minute=config.gemini.requests_per_minute,
            whisper=config.whisper,
        )

    def run(self, job: Job, config: AppConfig) -> Job:
        if not job.chunks:
            raise ValueError("No audio chunks to transcribe (did extract_audio run?).")
        provider = self._get_provider(config)
        chunks = sorted(job.chunks, key=lambda c: c.index)
        total = len(chunks)
        tag = getattr(provider, "cache_tag", "gemini")
        results: list[tuple[Chunk, list[Segment]]] = []

        for number, chunk in enumerate(chunks, start=1):
            cache = _transcript_path(job, chunk)
            local = _load_cached(cache, chunk, tag)
            if local is None:
                job.message = f"Transcribing chunk {number}/{total}"
                timer = getattr(provider, "timer", None)
                if (
                    timer is not None
                ):  # a separated voice may have lost the beeps: look in the original
                    original = job.work_dir / "chunks" / chunk.path.name
                    timer.beep_audio = (
                        original if original != chunk.path and original.is_file() else None
                    )
                try:
                    local = provider.transcribe(
                        chunk.path, language="zh", duration=chunk.end - chunk.start
                    )
                except WhisperError as exc:
                    raise RuntimeError(f"Speech timing failed: {exc}") from exc
                except QuotaExhaustedError as exc:
                    raise PauseRequested(
                        JobStatus.PAUSED,
                        "Gemini free quota used up while transcribing "
                        f"(chunk {number}/{total}; {number - 1} finished chunks are saved). "
                        f"Try again later with `video-translator resume {job.work_dir}`.",
                    ) from exc
                _save_cached(cache, chunk, local, tag)
            else:
                job.message = f"Transcribing chunk {number}/{total} (already done)"
            shifted = [
                Segment(start=s.start + chunk.start, end=s.end + chunk.start, text_zh=s.text_zh)
                for s in local
            ]
            results.append((chunk, shifted))

        job.segments = merge_chunk_segments(results)
        notes = [n for n in getattr(provider, "notes", []) if n]
        job.message = f"Transcribed {len(job.segments)} segments from {total} chunks" + (
            f" ({'; '.join(notes)})" if notes else ""
        )
        return job
