"""Mix the Vietnamese voice track with the original audio, ducking the original under speech.

Inputs:  artifacts['audio'] (original), artifacts['voice_track'], segments' aligned clips
Outputs: artifacts['mixed_audio'] (``<work_dir>/mixed.wav``, 48 kHz stereo 16-bit)

Ducking is a volume envelope on the original audio: ``ducked_volume`` while the voice speaks
(time ranges of the aligned clips), ``background_volume`` in the gaps, with 150 ms
attack/release ramps. Nearby ranges (closer than the ramp time) are merged. The envelope is
split over a chain of ``volume`` filters with ~40 ranges each (each active only in its own time
window), and the graph is passed as a script file, so the command line stays short on Windows
however long the video is. After amix (normalize=0) a single-pass loudnorm evens the level.

With voice separation (``job.separate_voice``) the mix is built from the separated parts: the
background music (``artifacts['background']``) replaces the original sound, and the separated
Chinese voice (``artifacts['vocals']``) is added at ``audio.original_voice_volume`` (0 by
default, so only the music and the Vietnamese voice remain). Both parts are ducked and have the
bleep tones silenced like the original sound.

Bleep tones (a censored word replaced by a steady tone in the original) are found in the chunk
audio files and silenced in the original completely, so only the Vietnamese voice says the word
(setting ``audio.remove_beeps: false`` keeps them).
"""

from __future__ import annotations

import wave
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import run_ffmpeg_filter_file

BEEP_MUTE_PAD = 0.05  # extra silence on each side of a detected beep
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


def mute_filters(spans: list[tuple[float, float]]) -> list[str]:
    """``volume=0`` filters that silence the original exactly inside ``spans``."""
    filters: list[str] = []
    for i in range(0, len(spans), RANGES_PER_FILTER):
        group = spans[i : i + RANGES_PER_FILTER]
        terms = "+".join(
            f"between(t,{max(a - BEEP_MUTE_PAD, 0.0):.3f},{b + BEEP_MUTE_PAD:.3f})"
            for a, b in group
        )
        filters.append(f"volume=0:enable='{terms}'")
    return filters


def merge_spans(spans: list[tuple[float, float]], gap: float = 0.1) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def find_beep_spans(job: Job) -> list[tuple[float, float]]:
    """Beep tones in the original audio, as ``(start, end)`` seconds from the video start."""
    from video_translator.providers.asr.whisper_timing import (
        WhisperError,
        decode_audio_16k,
        detect_beeps,
    )

    def original(path: Path) -> Path:
        # with voice separation job.chunks hold the separated voice: the beep may be missing there
        first = job.work_dir / "chunks" / path.name
        return first if first.is_file() else path

    sources: list[tuple[Path, float]] = [
        (original(Path(c.path)), c.start) for c in job.chunks if Path(c.path).is_file()
    ]
    if not sources and job.artifacts.get("audio"):
        sources = [(Path(job.artifacts["audio"]), 0.0)]
    spans: list[tuple[float, float]] = []
    for path, offset in sources:
        try:
            found = detect_beeps(decode_audio_16k(path))
        except WhisperError:
            continue
        spans.extend((offset + a, offset + b) for a, b in found)
    return merge_spans(spans)


def effective_audio(config: AppConfig, overrides: dict[str, float] | None = None):
    """The ``AudioConfig`` with the volumes chosen in the editor (``job.audio_overrides``)."""
    known = {k: v for k, v in (overrides or {}).items() if k in type(config.audio).model_fields}
    return config.audio.model_copy(update=known)


def build_graph(
    ranges: list[tuple[float, float]],
    config: AppConfig,
    overrides: dict[str, float] | None = None,
    mute: list[tuple[float, float]] | None = None,
    vocals: bool = False,
) -> str:
    """The ffmpeg filter graph. Inputs: 0 = original sound (or the separated background music),
    1 = the Vietnamese voice track, 2 (only with ``vocals``) = the separated Chinese voice."""
    audio = effective_audio(config, overrides)
    bg = audio.background_volume
    ratio = min(audio.ducked_volume / bg, 1.0) if bg > 0 else 1.0
    bg_chain = [
        f"aresample={OUT_RATE}",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        *mute_filters(mute or []),
        f"volume={bg:.4f}",
        *duck_filters(ranges, ratio),
    ]
    voice_chain = [
        f"aresample={OUT_RATE}",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        f"volume={audio.voice_volume:.4f}",
    ]
    zh_chain = [
        f"aresample={OUT_RATE}",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        *mute_filters(mute or []),
        f"volume={audio.original_voice_volume:.4f}",
        *duck_filters(ranges, ratio),
    ]
    zh_part = f"[2:a]{','.join(zh_chain)}[zh];\n" if vocals else ""
    inputs, count = ("[bg][voice][zh]", 3) if vocals else ("[bg][voice]", 2)
    return (
        f"[0:a]{','.join(bg_chain)}[bg];\n"
        f"[1:a]{','.join(voice_chain)}[voice];\n"
        f"{zh_part}"
        f"{inputs}amix=inputs={count}:duration=first:normalize=0:dropout_transition=0[mix];\n"
        f"[mix]loudnorm=I=-16:TP=-1.5:LRA=50,aresample={OUT_RATE}[out]"
    )


def separated_parts(job: Job) -> tuple[Path, Path] | None:
    """``(background, vocals)`` when this job separated the voice and both files exist."""
    if not job.separate_voice:
        return None
    background, vocals = job.artifacts.get("background"), job.artifacts.get("vocals")
    if background and vocals and Path(background).is_file() and Path(vocals).is_file():
        return Path(background), Path(vocals)
    return None


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
        beeps = find_beep_spans(job) if config.audio.remove_beeps else []
        parts = separated_parts(job)
        music, vocals = parts if parts else (Path(original), None)
        use_vocals = vocals is not None and (
            effective_audio(config, job.audio_overrides).original_voice_volume > 0
        )
        script = job.work_dir / "mix_filter.txt"
        script.write_text(
            build_graph(ranges, config, job.audio_overrides, beeps, vocals=use_vocals),
            encoding="utf-8",
        )
        out = job.work_dir / "mixed.wav"
        tmp = job.work_dir / "mixed.part.wav"
        run_ffmpeg_filter_file(
            ["-i", music, "-i", voice, *(["-i", vocals] if use_vocals else [])],
            script,
            ["-map", "[out]", "-c:a", "pcm_s16le", tmp],
            desc="mix voice and background",
        )
        tmp.replace(out)
        job.artifacts["mixed_audio"] = out
        note = f", {len(beeps)} beeps silenced" if beeps else ""
        if parts:
            note += ", Chinese voice kept" if use_vocals else ", Chinese voice removed"
        job.message = f"Mixed audio ready ({len(ranges)} speech passages ducked{note})"
        return job
