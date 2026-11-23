"""Fit each synthesized clip into its time slot and build the full-length voice track.

Inputs:  job.segments (audio_path, start, end), artifacts['audio'] (for the total length)
Outputs: segment.aligned_path (``<work_dir>/aligned/seg_XXXXX.wav``, 48 kHz mono 16-bit),
         artifacts['voice_track'] (``<work_dir>/voice_track.wav``, silence + clips at their
         start times, same length as the original audio).

Fitting rules (the third step of fitting a line; the translator already wrote it to fit and
rewrote lines that were too long). ``slot`` = ``end - start``; the clip may run up to 0.3 s
past the slot, never past 0.1 s before the next clip starts (the "window"):

* clip longer than the window: sped up (ffmpeg ``atempo``) by the needed factor, up to 1.2x. If
  that is not enough it is rushed up to 1.35x. If it still does not fit, the flag ``too_long`` is
  set and the overflow is allowed to run into the following gap; it is only cut (80 ms
  fade-out, flag ``truncated``) if it would run into the next clip.
* clip shorter than 0.9 of the slot: slowed down to fill it, at most to 0.85x.
"""

from __future__ import annotations

import array
import os
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job, Segment
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import probe_duration, run_ffmpeg

SAMPLE_RATE = 48000
GAP = 0.1  # keep this much silence before the next clip when stretching into the gap
STRETCH_MAX = 1.2  # the normal speed-up
MAX_SPEEDUP = 1.35  # rushed, only when the normal speed-up is not enough
OVERRUN = 0.3  # a clip may run this long past its slot when the next line starts later
FIT_FLAGS = ("too_long", "truncated", "needs_realign")
MIN_SLOWDOWN = 0.85
SHORT_RATIO = 0.9
FADE = 0.08
MIN_LIMIT = 0.3
SILENCE_CHUNK = 1 << 20


def atempo_chain(tempo: float) -> list[str]:
    """ffmpeg ``atempo`` filters for ``tempo`` (each filter accepts 0.5 to 2.0)."""
    filters: list[str] = []
    while tempo > 2.0:
        filters.append("atempo=2.0")
        tempo /= 2.0
    while tempo < 0.5:
        filters.append("atempo=0.5")
        tempo /= 0.5
    if abs(tempo - 1.0) > 1e-3:
        filters.append(f"atempo={tempo:.5f}")
    return filters


def plan_clip(
    duration: float, slot: float, available: float, limit: float | None
) -> tuple[float, list[str], float | None]:
    """Decide ``(tempo, flags, cut_to)`` for a clip of ``duration`` seconds.

    ``limit`` is the longest the clip may be (until the next clip starts), None for no limit;
    ``cut_to`` is the length to truncate to (after tempo), or None.
    """
    flags: list[str] = []
    tempo = 1.0
    window = max(slot, min(slot + OVERRUN, available))
    if duration > window + 1e-6:
        needed = duration / window
        tempo = min(needed, STRETCH_MAX)
        if duration / tempo > window + 1e-6:  # a normal speed-up is not enough: rush it
            tempo = min(needed, MAX_SPEEDUP)
            if needed > MAX_SPEEDUP + 1e-6:
                flags.append("too_long")
    elif slot > 0 and duration < SHORT_RATIO * slot:
        tempo = max(MIN_SLOWDOWN, duration / slot)
    new_duration = duration / tempo
    cut_to = None
    if limit is not None and new_duration > limit:
        cut_to = limit
        if "too_long" not in flags:
            flags.append("too_long")
        flags.append("truncated")
    return tempo, flags, cut_to


def plan_clip_with_speed(
    duration: float, speed: float, available: float, limit: float | None
) -> tuple[float, list[str], float | None]:
    """Like ``plan_clip`` but with the user's tempo ``speed`` on the raw clip.

    The clip is still cut (80 ms fade-out) if it would run into the next clip.
    """
    flags: list[str] = []
    new_duration = duration / speed
    if new_duration > available + 1e-6:
        flags.append("too_long")
    cut_to = None
    if limit is not None and new_duration > limit:
        cut_to = limit
        if "too_long" not in flags:
            flags.append("too_long")
        flags.append("truncated")
    return speed, flags, cut_to


def _align_clip(
    src: Path,
    dst: Path,
    slot: float,
    available: float,
    limit: float | None,
    speed: float | None = None,
):
    duration = probe_duration(src)
    if speed is not None:
        tempo, flags, cut_to = plan_clip_with_speed(duration, speed, available, limit)
    else:
        tempo, flags, cut_to = plan_clip(duration, slot, available, limit)
    filters = atempo_chain(tempo)
    if cut_to is not None:
        filters.append(f"atrim=end={cut_to:.4f}")
        filters.append(f"afade=t=out:st={max(cut_to - FADE, 0):.4f}:d={FADE}")
    args: list[str | Path] = ["-i", src]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-ac", "1", "-ar", SAMPLE_RATE, "-c:a", "pcm_s16le", dst]
    run_ffmpeg(args, desc=f"align {src.name}")
    return flags


def has_clip(seg: Segment) -> bool:
    return bool(seg.audio_path and Path(seg.audio_path).is_file())


def clip_window(
    segs: list[Segment], index: int, total: float | None
) -> tuple[float, float, float | None]:
    """``(slot, available, limit)`` for segment ``index`` among the segments that have a clip.

    The neighbours are the segments with a raw clip, ordered by start time (the same rule the
    align stage uses), so one line can be re-fitted without touching the others.
    """
    order = sorted(
        (i for i, s in enumerate(segs) if has_clip(s) or i == index), key=lambda i: segs[i].start
    )
    pos = order.index(index)
    seg = segs[index]
    slot = max(seg.end - seg.start, 0.05)
    if pos + 1 < len(order):
        next_start = segs[order[pos + 1]].start
        return (
            slot,
            max(slot, next_start - GAP - seg.start),
            max(next_start - seg.start, MIN_LIMIT),
        )
    available = slot if total is None else max(slot, total - seg.start)
    return slot, available, None


def _original_duration(job: Job) -> float | None:
    audio = job.artifacts.get("audio")
    return probe_duration(audio) if audio and Path(audio).is_file() else None


def fit_segment(job: Job, index: int, total: float | None = None) -> Segment:
    """Re-create the aligned clip of ONE segment from its raw clip, honouring ``segment.speed``.

    Updates ``aligned_path`` and the fit flags (``too_long``, ``truncated``, ``needs_realign``
    are recomputed). The voice track is not rebuilt; call ``rebuild_voice_track`` for that.
    """
    seg = job.segments[index]
    if not has_clip(seg):
        raise FileNotFoundError(f"Line {index + 1} has no voice clip yet.")
    if total is None:
        total = _original_duration(job)
    slot, available, limit = clip_window(job.segments, index, total)
    aligned_dir = job.work_dir / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)
    dst = aligned_dir / f"seg_{index:05d}.wav"
    tmp = dst.with_name(f"seg_{index:05d}.part.wav")
    try:
        flags = _align_clip(Path(seg.audio_path), tmp, slot, available, limit, seg.speed)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    seg.aligned_path = dst
    seg.flags = [f for f in seg.flags if f not in FIT_FLAGS] + flags
    return seg


def _mix_into(buf: bytearray, clip: bytes) -> bytearray:
    """Add ``clip`` (16-bit pcm) onto ``buf`` from the start; result is as long as the longer."""
    overlap = min(len(buf), len(clip)) // 2 * 2
    if overlap:
        a = array.array("h")
        a.frombytes(bytes(buf[:overlap]))
        b = array.array("h")
        b.frombytes(clip[:overlap])
        mixed = array.array("h", (max(-32768, min(32767, x + y)) for x, y in zip(a, b)))
        buf[:overlap] = mixed.tobytes()
    if len(clip) > len(buf):
        buf.extend(clip[len(buf) :])
    return buf


def build_voice_track(
    placements: list[tuple[float, Path]], total_seconds: float, out_path: Path
) -> Path:
    """Write a mono 48 kHz wav of ``total_seconds`` with each clip at its start time.

    Streams in order of start time; memory stays small and no ffmpeg process is needed.
    Overlapping clips are summed.
    """
    total_frames = round(total_seconds * SAMPLE_RATE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    pos = 0  # frames written
    buf = bytearray()  # audio already placed from frame ``pos`` onwards
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)

        def flush_to(target: int) -> None:
            nonlocal pos, buf
            target = min(target, total_frames)
            n = target - pos
            if n <= 0:
                return
            take = min(len(buf) // 2, n)
            out.writeframes(bytes(buf[: take * 2]))
            buf = buf[take * 2 :]
            remaining = n - take
            while remaining > 0:
                step = min(remaining, SILENCE_CHUNK // 2)
                out.writeframes(bytes(step * 2))
                remaining -= step
            pos = target

        for start, path in sorted(placements, key=lambda p: p[0]):
            flush_to(round(start * SAMPLE_RATE))
            with wave.open(str(path), "rb") as clip:
                if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (
                    1,
                    2,
                    SAMPLE_RATE,
                ):
                    raise ValueError(f"Unexpected format in aligned clip {path}")
                data = clip.readframes(clip.getnframes())
            buf = _mix_into(buf, data)
        flush_to(total_frames)
    os.replace(tmp, out_path)
    return out_path


def rebuild_voice_track(job: Job, total: float | None = None) -> Path:
    """Rebuild ``artifacts['voice_track']`` from the current aligned clips of the job."""
    segs = job.segments
    if total is None:
        total = _original_duration(job)
    if total is None:
        total = max((s.end for s in segs), default=0.0)
    placements = [
        (seg.start, Path(seg.aligned_path))
        for seg in sorted(segs, key=lambda s: s.start)
        if seg.aligned_path and Path(seg.aligned_path).is_file()
    ]
    job.artifacts["voice_track"] = build_voice_track(
        placements, total, job.work_dir / "voice_track.wav"
    )
    return Path(job.artifacts["voice_track"])


class AlignStage(Stage):
    name = "align"

    def run(self, job: Job, config: AppConfig) -> Job:
        segs = job.segments
        audio = job.artifacts.get("audio")
        total = probe_duration(audio) if audio and Path(audio).is_file() else None

        with_clip = [i for i, s in enumerate(segs) if s.audio_path and Path(s.audio_path).is_file()]
        order = sorted(with_clip, key=lambda i: segs[i].start)
        aligned_dir = job.work_dir / "aligned"
        aligned_dir.mkdir(parents=True, exist_ok=True)

        tasks = []
        for pos, i in enumerate(order):
            seg = segs[i]
            dst = aligned_dir / f"seg_{i:05d}.wav"
            if seg.aligned_path and Path(seg.aligned_path).is_file():
                continue
            slot = max(seg.end - seg.start, 0.05)
            if pos + 1 < len(order):
                next_start = segs[order[pos + 1]].start
                available = max(slot, next_start - GAP - seg.start)
                limit: float | None = max(next_start - seg.start, MIN_LIMIT)
            else:
                end_of_audio = total if total is not None else None
                available = slot if end_of_audio is None else max(slot, end_of_audio - seg.start)
                limit = None
            tasks.append((i, Path(seg.audio_path), dst, slot, available, limit, seg.speed))

        def work(task):
            i, src, dst, slot, available, limit, speed = task
            return i, dst, _align_clip(src, dst, slot, available, limit, speed)

        workers = max(1, min(4, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, dst, flags in pool.map(work, tasks):
                seg = segs[i]
                seg.aligned_path = dst
                for flag in flags:
                    if flag not in seg.flags:
                        seg.flags.append(flag)
        job.save()

        placements = [
            i for i in order if segs[i].aligned_path and Path(segs[i].aligned_path).is_file()
        ]
        rebuild_voice_track(job, total)
        job.message = self._summary(segs, len(placements))
        return job

    @staticmethod
    def _summary(segs: list[Segment], n_clips: int) -> str:
        too_long = [i + 1 for i, s in enumerate(segs) if "too_long" in s.flags]
        truncated = [i + 1 for i, s in enumerate(segs) if "truncated" in s.flags]
        parts = [f"Aligned {n_clips} lines"]
        if too_long:
            parts.append(
                f"{len(too_long)} lines were too long and sped up (lines {_short(too_long)})"
            )
        if truncated:
            parts.append(f"{len(truncated)} lines were cut at the end (lines {_short(truncated)})")
        return "; ".join(parts)


def _short(numbers: list[int], limit: int = 20) -> str:
    text = ", ".join(str(n) for n in numbers[:limit])
    return text + (", ..." if len(numbers) > limit else "")
