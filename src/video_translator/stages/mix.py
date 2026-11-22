"""Mix the Vietnamese voice track with the original audio, ducking the original under speech.

Inputs:  artifacts['audio'] (original), artifacts['voice_track'], segments' aligned clips
Outputs: artifacts['mixed_audio'] (``<work_dir>/mixed.wav``, 48 kHz stereo 16-bit)

Ducking is a volume envelope on the original audio: ``ducked_volume`` while the voice speaks
(time ranges of the aligned clips), ``background_volume`` in the gaps, with 150 ms
attack/release ramps. Nearby ranges (closer than the ramp time) are merged. The envelope is
split over a chain of ``volume`` filters with ~40 ranges each (each active only in its own time
window), and the graph is passed as a script file, so the command line stays short on Windows
however long the video is. After amix (normalize=0) a single-pass loudnorm evens the level.
"""

from __future__ import annotations

import wave
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import run_ffmpeg

ATTACK = 0.15
RELEASE = 0.15
RANGES_PER_FILTER = 40
OUT_RATE = 48000


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def speech_ranges(job: Job) -> list[tuple[float, float]]:
    """Merged ``(start, end)`` time ranges covered by the aligned clips."""
    raw: list[tuple[float, float]] = []
    for seg in job.segments:
        if seg.aligned_path and Path(seg.aligned_path).is_file():
            raw.append((seg.start, seg.start + _wav_duration(Path(seg.aligned_path))))
    raw.sort()
    merged: list[list[float]] = []
    for a, b in raw:
        if merged and a - merged[-1][1] < ATTACK + RELEASE:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def duck_filters(ranges: list[tuple[float, float]], ratio: float) -> list[str]:
    """``volume`` filters lowering the level to ``ratio`` (0..1) inside ``ranges``."""
    if ratio >= 1.0 or not ranges:
        return []
    depth = 1.0 - ratio
    filters: list[str] = []
    for i in range(0, len(ranges), RANGES_PER_FILTER):
        group = ranges[i : i + RANGES_PER_FILTER]
        terms = []
        for a, b in group:
            lo, hi = a - ATTACK, b + RELEASE
            terms.append(f"max(0,min(1,min((t-{lo:.3f})/{ATTACK},({hi:.3f}-t)/{RELEASE})))")
        window_lo = max(group[0][0] - ATTACK - 0.05, 0.0)
        window_hi = group[-1][1] + RELEASE + 0.05
        expr = f"1-{depth:.4f}*({'+'.join(terms)})"
        filters.append(
            f"volume=volume='{expr}':eval=frame:enable='between(t,{window_lo:.3f},{window_hi:.3f})'"
        )
    return filters


def build_graph(ranges: list[tuple[float, float]], config: AppConfig) -> str:
    audio = config.audio
    bg = audio.background_volume
    ratio = min(audio.ducked_volume / bg, 1.0) if bg > 0 else 1.0
    bg_chain = [
        f"aresample={OUT_RATE}",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        f"volume={bg:.4f}",
        *duck_filters(ranges, ratio),
    ]
    voice_chain = [
        f"aresample={OUT_RATE}",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        f"volume={audio.voice_volume:.4f}",
    ]
    return (
        f"[0:a]{','.join(bg_chain)}[bg];\n"
        f"[1:a]{','.join(voice_chain)}[voice];\n"
        "[bg][voice]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[mix];\n"
        f"[mix]loudnorm=I=-16:TP=-1.5:LRA=50,aresample={OUT_RATE}[out]"
    )


class MixStage(Stage):
    name = "mix"

    def run(self, job: Job, config: AppConfig) -> Job:
        original = job.artifacts.get("audio")
        voice = job.artifacts.get("voice_track")
        if not original or not Path(original).is_file():
            raise FileNotFoundError("The original audio (artifacts['audio']) is missing.")
        if not voice or not Path(voice).is_file():
            raise FileNotFoundError("The voice track (artifacts['voice_track']) is missing.")
        ranges = speech_ranges(job)
        script = job.work_dir / "mix_filter.txt"
        script.write_text(build_graph(ranges, config), encoding="utf-8")
        out = job.work_dir / "mixed.wav"
        tmp = job.work_dir / "mixed.part.wav"
        run_ffmpeg(
            [
                "-i",
                original,
                "-i",
                voice,
                "-filter_complex_script",
                script,
                "-map",
                "[out]",
                "-c:a",
                "pcm_s16le",
                tmp,
            ],
            desc="mix voice and background",
        )
        tmp.replace(out)
        job.artifacts["mixed_audio"] = out
        job.message = f"Mixed audio ready ({len(ranges)} speech passages ducked)"
        return job
