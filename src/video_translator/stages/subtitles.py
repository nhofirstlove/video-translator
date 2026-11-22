"""Build the Vietnamese .srt file from the translated segments.

Inputs:  job.segments (text_vi), config.subtitles
Outputs: artifacts['srt'] (``<work_dir>/subtitles/<stem>.vi.srt``)

Burning subtitles into the video (``config.subtitles.burn_in``) is a later version.
"""

from __future__ import annotations

import logging

from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.paths import safe_name
from video_translator.stages.base import Stage
from video_translator.utils.srt import segments_to_srt

log = logging.getLogger(__name__)


class SubtitlesError(RuntimeError):
    """The segments are not ready for subtitles (e.g. not translated yet)."""


class SubtitlesStage(Stage):
    name = "subtitles"

    def run(self, job: Job, config: AppConfig) -> Job:
        if config.subtitles.burn_in:
            job.message = (
                "Burning subtitles into the video is not supported yet; writing .srt only."
            )
            log.warning(job.message)
        if not config.subtitles.srt:
            return job

        speech = [s for s in job.segments if s.text_zh.strip() or s.text_vi.strip()]
        if not speech:
            raise SubtitlesError("There are no segments to write subtitles for.")
        missing = [s for s in speech if not s.text_vi.strip()]
        if missing:
            first = missing[0]
            raise SubtitlesError(
                f"{len(missing)} of {len(speech)} segments have no Vietnamese text yet "
                f"(first at {first.start:.1f}s). Run the translate stage first."
            )

        out_dir = job.work_dir / "subtitles"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{safe_name(job.input_path.stem)}.vi.srt"
        content = segments_to_srt(job.segments, text_attr="text_vi")
        # UTF-8 *without* BOM, "\n" newlines: players (VLC, mpv, YouTube) read this correctly.
        # write_bytes avoids Windows turning "\n" into "\r\n".
        path.write_bytes(content.encode("utf-8"))
        job.artifacts["srt"] = path
        job.message = f"Wrote subtitles to {path.name}"
        return job
