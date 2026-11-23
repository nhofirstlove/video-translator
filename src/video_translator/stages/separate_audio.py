"""Split the voice from the music and effects (only when the job asks for it).

Inputs:  artifacts['audio'], job.chunks (cut from the original sound)
Outputs: artifacts['vocals'], artifacts['background'] (+ small ``*_preview`` m4a copies for
         the editor), and ``job.chunks`` re-cut from the voice
         alone, so the timing and the transcription listen to the voice only. The original
         chunks stay in ``chunks/`` (the beep detector still listens to them), and the final mix
         still uses the original audio.

A job without ``separate_voice`` passes through untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

from video_translator.config import AppConfig
from video_translator.models import Chunk, Job
from video_translator.providers import registry
from video_translator.providers.separation.base import SeparationProvider
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import run_ffmpeg

CHUNK_SAMPLE_RATE = 16000


def _done(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


class SeparateAudioStage(Stage):
    name = "separate_audio"

    def __init__(self, provider: SeparationProvider | None = None) -> None:
        """``provider`` is for tests; normally it is the Demucs provider."""
        self._provider = provider

    def _get_provider(self, job: Job, config: AppConfig) -> SeparationProvider:
        if self._provider is not None:
            return self._provider

        def progress(done: int, total: int) -> None:
            job.message = f"Separating the voice from the music: part {done} of {total}"

        from video_translator.utils.device import resolve_device

        cls = registry.get("separation", "demucs")
        return cls(
            device=resolve_device(config.device), model=config.separation.model, progress=progress
        )

    @staticmethod
    def _preview(job: Job, source: Path, kind: str) -> Path:
        """A small aac copy the editor plays next to the video (the wav is far too big)."""
        target = source.with_name(f"{kind}_preview.m4a")
        if not _done(target):
            tmp = target.with_name(f"{kind}_preview.part.m4a")
            try:
                run_ffmpeg(
                    ["-i", source, "-vn", "-ac", 2, "-c:a", "aac", "-b:a", "96k", tmp],
                    desc=f"make the {kind} preview",
                )
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        return target

    def run(self, job: Job, config: AppConfig) -> Job:
        if not job.separate_voice:
            return job
        if "audio" not in job.artifacts or not job.chunks:
            raise ValueError("No audio to separate (did extract_audio run?).")
        provider = self._get_provider(job, config)
        vocals, background = provider.separate(job.artifacts["audio"], job.work_dir / "separation")
        job.artifacts["vocals"], job.artifacts["background"] = vocals, background
        for kind, source in (("vocals", vocals), ("background", background)):
            job.artifacts[f"{kind}_preview"] = self._preview(job, source, kind)

        chunk_dir = job.work_dir / "chunks_vocals"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        recut: list[Chunk] = []
        for chunk in sorted(job.chunks, key=lambda c: c.index):
            path = chunk_dir / f"chunk_{chunk.index:03d}.wav"
            if not _done(path):
                tmp = path.with_name(f"{path.stem}.part{path.suffix}")
                try:
                    run_ffmpeg(
                        [
                            "-ss", f"{chunk.start:.3f}", "-t", f"{chunk.end - chunk.start:.3f}",
                            "-i", vocals, "-ar", CHUNK_SAMPLE_RATE, "-ac", 1,
                            "-c:a", "pcm_s16le", tmp,
                        ],
                        desc=f"cut voice chunk {chunk.index}",
                    )  # fmt: skip
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            recut.append(Chunk(index=chunk.index, start=chunk.start, end=chunk.end, path=path))
        job.chunks = recut
        job.message = "Voice separated from the music"
        return job
