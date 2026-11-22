"""Fit each synthesized clip into its time slot and build the full-length voice track.

Inputs:  job.segments (audio_path, start, end), artifacts['audio'] (for the total length)
Outputs: segment.aligned_path (``<work_dir>/aligned/seg_XXXXX.wav``, 48 kHz mono 16-bit),
         artifacts['voice_track'] (``<work_dir>/voice_track.wav``, silence + clips at their
         start times, same length as the original audio).

Fitting rules (``available`` = time until 0.1 s before the next clip starts, at least the
slot ``end - start``):

* clip longer than available: speed up (ffmpeg ``atempo``) by the needed factor, at most
  1.3x. If still too long the flag ``too_long`` is set and the overflow is allowed to run
  into the following gap; it is only cut (80 ms fade-out, flag ``truncated``) if it would
  run into the next clip.
* clip shorter than 0.6 of the slot: slowed to 0.9x. Otherwise left as is.
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
MAX_SPEEDUP = 1.3
MIN_SLOWDOWN = 0.9
SHORT_RATIO = 0.6
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
    if duration > available:
        needed = duration / max(available, 1e-3)
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


def _align_clip(src: Path, dst: Path, slot: float, available: float, limit: float | None):
    duration = probe_duration(src)
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
            tasks.append((i, Path(seg.audio_path), dst, slot, available, limit))

        def work(task):
            i, src, dst, slot, available, limit = task
            return i, dst, _align_clip(src, dst, slot, available, limit)

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
            (segs[i].start, Path(segs[i].aligned_path))
            for i in order
            if segs[i].aligned_path and Path(segs[i].aligned_path).is_file()
        ]
        if total is None:
            total = max((s.end for s in segs), default=0.0)
        job.artifacts["voice_track"] = build_voice_track(
            placements, total, job.work_dir / "voice_track.wav"
        )
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
