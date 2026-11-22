"""Pull the audio track out of the input video with ffmpeg and cut it into chunks.

Inputs:  job.input_path (video)
Outputs: artifacts['audio'] (full original audio: 44.1 kHz stereo 16-bit wav) and
         job.chunks (16 kHz mono wav files for speech recognition, with a small overlap).

The video is decoded only once; the chunks are cut from the extracted wav with fast
input seeking. Existing non-empty outputs are reused, so the stage can be resumed.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import (
    FFmpegError,
    has_audio_stream,
    probe_duration,
    run_ffmpeg,
)

CHUNK_OVERLAP_SECONDS = 5.0
CHUNK_SAMPLE_RATE = 16000
AUDIO_SAMPLE_RATE = 44100


def _done(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _ffmpeg_to(target: Path, args: list[str | Path], desc: str) -> None:
    """Run ffmpeg writing to a temp file, then move it onto ``target`` (no partial outputs)."""
    tmp = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        run_ffmpeg([*args, tmp], desc=desc)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def plan_chunks(total: float, chunk_seconds: float, overlap: float) -> list[tuple[float, float]]:
    """(start, end) ranges: each chunk runs ``overlap`` seconds into the next one.

    A tail shorter than the overlap is not a chunk of its own (the previous one already
    covers it); the last chunk always ends at ``total``.
    """
    count = max(1, math.ceil((total - overlap) / chunk_seconds))
    ranges = []
    for i in range(count):
        start = i * chunk_seconds
        end = total if i == count - 1 else min(start + chunk_seconds + overlap, total)
        ranges.append((start, end))
    return ranges


class ExtractAudioStage(Stage):
    name = "extract_audio"

    def run(self, job: Job, config: AppConfig) -> Job:
        video = Path(job.input_path)
        if not video.is_file():
            raise FileNotFoundError(f"Input video not found: {video}")

        audio_path = job.work_dir / "audio" / "audio.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        if not _done(audio_path):
            if not has_audio_stream(video):
                raise FFmpegError(f"The video has no audio stream: {video.name}")
            _ffmpeg_to(
                audio_path,
                [
                    "-i", video, "-map", "0:a:0", "-vn",
                    "-ar", AUDIO_SAMPLE_RATE, "-ac", 2, "-c:a", "pcm_s16le",
                ],
                "extract audio",
            )  # fmt: skip
        job.artifacts["audio"] = audio_path

        total = probe_duration(audio_path)
        chunk_dir = job.work_dir / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[Chunk] = []
        for index, (start, end) in enumerate(
            plan_chunks(total, config.chunk_minutes * 60, CHUNK_OVERLAP_SECONDS)
        ):
            path = chunk_dir / f"chunk_{index:03d}.wav"
            if not _done(path):
                _ffmpeg_to(
                    path,
                    [
                        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", audio_path,
                        "-ar", CHUNK_SAMPLE_RATE, "-ac", 1, "-c:a", "pcm_s16le",
                    ],
                    f"cut chunk {index}",
                )  # fmt: skip
            chunks.append(Chunk(index=index, start=start, end=end, path=path))
        job.chunks = chunks
        return job
