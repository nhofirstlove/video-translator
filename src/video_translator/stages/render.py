"""Mux the original video with the new audio with ffmpeg.

Inputs:  job.input_path, artifacts['mixed_audio'], optional artifacts['srt']
Outputs: artifacts['output'] (``<output_dir>/<name>.vi.mp4``) and, if there are
         subtitles, artifacts['srt_output'] (``<output_dir>/<name>.vi.srt``).

The video stream is copied (no re-encode), unless the blur box is on (``config.blur`` or the
choice saved for this video): then the box is blurred, the Vietnamese subtitles are burned in
on top of it and the video is re-encoded (H.264, slower). The audio is encoded to AAC; if it is
shorter than the video it is padded with silence, and the result never outlasts the video.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from video_translator.blur import (
    build_ass,
    build_filter_graph,
    effective_blur,
    resolve_box,
)
from video_translator.config import AppConfig
from video_translator.models import Job
from video_translator.paths import resolve_path, safe_name
from video_translator.stages.base import Stage
from video_translator.utils.ffmpeg import probe_duration, probe_video_size, run_ffmpeg

AUDIO_BITRATE = "192k"


class RenderStage(Stage):
    name = "render"

    def run(self, job: Job, config: AppConfig) -> Job:
        video = Path(job.input_path)
        mixed = job.artifacts.get("mixed_audio")
        if mixed is None or not Path(mixed).is_file():
            raise FileNotFoundError("No mixed audio to render (run the mix stage first).")
        if not video.is_file():
            raise FileNotFoundError(f"Input video not found: {video}")

        out_dir = resolve_path(config.paths.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_name(video.stem)
        target = out_dir / f"{stem}.vi.mp4"
        tmp = out_dir / f"{stem}.vi.part.mp4"
        blur = effective_blur(config, job.blur_overrides)
        try:
            if blur.enabled:
                self._render_with_blur(job, blur, video, mixed, tmp)
            else:
                run_ffmpeg(
                    [
                        "-i", video, "-i", mixed,
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                        "-af", "apad", "-t", f"{probe_duration(video):.3f}",
                        "-movflags", "+faststart", tmp,
                    ],
                    desc="render video",
                )  # fmt: skip
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        job.artifacts["output"] = target

        srt = job.artifacts.get("srt")
        if srt is not None and Path(srt).is_file():
            srt_target = out_dir / f"{stem}.vi.srt"
            shutil.copyfile(srt, srt_target)
            job.artifacts["srt_output"] = srt_target
        return job

    def _render_with_blur(self, job: Job, blur, video: Path, mixed, tmp: Path) -> None:
        width, height = probe_video_size(video)
        box = resolve_box(blur, width, height)
        ass = job.work_dir / "subtitles" / "blur.ass"
        ass.parent.mkdir(parents=True, exist_ok=True)
        ass.write_text(build_ass(job.segments, blur, box, width, height), encoding="utf-8")
        job.message = "Rendering with the blur box (the video is re-encoded, this takes a while)"
        job.save()
        run_ffmpeg(
            [
                "-i", video, "-i", mixed,
                "-filter_complex", build_filter_graph(blur, box, ass),
                "-map", "[v]", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                "-af", "apad", "-t", f"{probe_duration(video):.3f}",
                "-movflags", "+faststart", tmp,
            ],
            desc="render video with the blur box",
        )  # fmt: skip
