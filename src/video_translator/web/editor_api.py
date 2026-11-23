"""HTTP API of the interactive editor (see docs/editor-api.md for the contract).

Everything here edits a finished or paused job: lines (text and times), single-line voice
regeneration, per-line speed, volume settings, and "apply" which rebuilds subtitles and video
without synthesizing any voice. Edits are refused (409) while the job is running. Media is
served with HTTP Range support (Starlette's ``FileResponse`` implements 206/416) and only from
inside the work, input and output folders.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from video_translator.blur import Box, effective_blur, resolve_box
from video_translator.config import BlurConfig, ConfigError, save_local_override
from video_translator.models import Job, Segment
from video_translator.providers.tts.base import TTSError, TTSSetupError, TTSVoiceError
from video_translator.stages.align import FIT_FLAGS, fit_segment, has_clip
from video_translator.stages.mix import effective_audio
from video_translator.synth_line import LineSynthError, line_chars, synthesize_line
from video_translator.utils.ffmpeg import FFmpegError, probe_duration, probe_video_size
from video_translator.waveform import (
    BACKGROUND_PEAKS_PER_SECOND,
    VOICE_PEAKS_PER_SECOND,
    WaveformError,
    file_peaks,
)
from video_translator.web.assets import add_versions
from video_translator.web.jobs import InvalidEdit, JobError, JobManager, JobNotFound, JobStateError

MIN_LINE_SECONDS = 0.2
OVERLAP_TOLERANCE = 0.05
MIN_SPEED, MAX_SPEED = 0.7, 1.5
MAX_VOLUME = 2.0
NEEDS_REALIGN = "needs_realign"
NO_CACHE = {"Cache-Control": "no-store"}
MEDIA_HEADERS = {"Cache-Control": "no-cache"}

PLACEHOLDER_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Editor</title></head>'
    "<body><p>The editor page is not installed yet.</p></body></html>"
)


class BadRequest(JobError):
    status_code = 400


class UpstreamError(JobError):
    status_code = 502


class ServerProblem(JobError):
    status_code = 500


class EditorRoute(APIRoute):
    """Turns request validation problems into the contract's ``{"detail": "<text>"}``."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse({"detail": _plain(exc)}, status_code=422)

        return handler


def _plain(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        where = [str(p) for p in err.get("loc", ()) if p not in ("body", "path", "query")]
        label = f"{'.'.join(where)}: " if where else ""
        parts.append(f"{label}{err.get('msg', 'invalid value')}")
    return "Invalid request. " + "; ".join(parts) if parts else "Invalid request."


router = APIRouter(route_class=EditorRoute)


def get_manager(request: Request) -> JobManager:
    return request.app.state.jobs


def editor_page_html(web_dir: Path) -> str:
    page = web_dir / "templates" / "editor.html"
    try:
        return add_versions(page.read_text(encoding="utf-8"), web_dir / "static")
    except OSError:
        return PLACEHOLDER_HTML


# ----- helpers -----------------------------------------------------------------------------


def format_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    minutes, rest = divmod(total_ms, 60_000)
    hours, minutes = divmod(minutes, 60)
    head = f"{hours}:{minutes:02d}" if hours else f"{minutes:02d}"
    return f"{head}:{rest // 1000:02d}.{rest % 1000:03d}"


def _segment(job: Job, index: int) -> Segment:
    if not 0 <= index < len(job.segments):
        raise JobNotFound("There is no such line.")
    return job.segments[index]


def _resolve_inside(manager: JobManager, job: Job, raw: str | Path | None) -> Path | None:
    """The existing file ``raw`` points to, only if it lies inside the job/work/input/output
    folders (symlinks and ``..`` are resolved first)."""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = job.work_dir / path
    try:
        resolved = path.resolve()
        roots = [
            manager.work_root.resolve(),
            manager.input_root.resolve(),
            manager.output_root.resolve(),
            job.work_dir.resolve(),
        ]
    except OSError:
        return None
    if resolved.is_file() and any(resolved.is_relative_to(root) for root in roots):
        return resolved
    return None


def _clip_path(manager: JobManager, job: Job, seg: Segment) -> Path | None:
    """The clip to play: the aligned clip if present, else the raw TTS clip."""
    return _resolve_inside(manager, job, seg.aligned_path) or _resolve_inside(
        manager, job, seg.audio_path
    )


_DURATIONS: dict[tuple[str, int, int], float] = {}


def _probe_cached(path: Path) -> float | None:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if key not in _DURATIONS:
        try:
            _DURATIONS[key] = probe_duration(path)
        except (FFmpegError, OSError, ValueError):
            return None
        if len(_DURATIONS) > 256:
            _DURATIONS.pop(next(iter(_DURATIONS)))
    return _DURATIONS[key]


def _duration(manager: JobManager, job: Job) -> tuple[float, bool]:
    """``(seconds, known)``: length of the video; ``known`` is False when guessed from lines."""
    for raw in (job.artifacts.get("audio"), job.input_path):
        path = _resolve_inside(manager, job, raw)
        value = _probe_cached(path) if path else None
        if value:
            return value, True
    return max((s.end for s in job.segments), default=0.0), False


def segment_json(manager: JobManager, job: Job, index: int) -> dict[str, Any]:
    seg = job.segments[index]
    return {
        "index": index,
        "start": seg.start,
        "end": seg.end,
        "text_zh": seg.text_zh,
        "text_vi": seg.text_vi,
        "flags": list(seg.flags),
        "has_audio": _clip_path(manager, job, seg) is not None,
        "audio_stale": seg.audio_stale,
        "speed": seg.speed,
        "chars": line_chars(seg),
    }


def _stem_path(manager: JobManager, job: Job, kind: str) -> Path | None:
    """The small playable copy of a separated part (``vocals`` or ``background``), if any."""
    if not job.separate_voice:
        return None
    return _resolve_inside(manager, job, job.artifacts.get(f"{kind}_preview"))


def _audio_settings(manager: JobManager, job: Job) -> dict[str, Any]:
    audio = effective_audio(manager.config, job.audio_overrides)
    return {
        "voice_volume": audio.voice_volume,
        "background_volume": audio.background_volume,
        "ducked_volume": audio.ducked_volume,
        "original_voice_volume": audio.original_voice_volume,
        # true when the voice was separated from the music: the editor then shows both parts
        "has_stems": all(_stem_path(manager, job, k) is not None for k in ("vocals", "background")),
    }


_VIDEO_SIZES: dict[tuple[str, float], tuple[int, int]] = {}


def _video_size(manager: JobManager, job: Job) -> tuple[int, int] | None:
    path = _resolve_inside(manager, job, job.input_path)
    if path is None:
        return None
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key not in _VIDEO_SIZES:
        try:
            _VIDEO_SIZES[key] = probe_video_size(path)
        except (FFmpegError, OSError, ValueError):
            return None
        if len(_VIDEO_SIZES) > 64:
            _VIDEO_SIZES.pop(next(iter(_VIDEO_SIZES)))
    return _VIDEO_SIZES[key]


def _blur_settings(manager: JobManager, job: Job) -> dict[str, Any]:
    """This video's blur settings (global default + its own choices) and the real box in pixels."""
    blur = effective_blur(manager.config, job.blur_overrides)
    size = _video_size(manager, job)
    settings = blur.model_dump()
    if size is not None:
        box = resolve_box(blur, size[0], size[1])
        settings.update(x=box.x, y=box.y, width=box.width, height=box.height)
    return {
        "settings": settings,
        "defaults": manager.config.blur.model_dump(),
        "video": {"width": size[0], "height": size[1]} if size else None,
        "customised": bool(job.blur_overrides),
    }


def _finite(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(value):
        raise InvalidEdit(f"The {label} is not a valid number.")
    return round(float(value), 3)


# ----- request bodies ----------------------------------------------------------------------


class SegmentPatch(BaseModel):
    start: float | None = None
    end: float | None = None
    text_vi: str | None = None


class RegenerateBody(BaseModel):
    confirm: bool = False  # accepted for compatibility; not required any more


class SpeedBody(BaseModel):
    speed: float | None = Field(...)  # null puts the line back to automatic


class AudioSettingsBody(BaseModel):
    voice_volume: float
    background_volume: float
    ducked_volume: float
    original_voice_volume: float | None = None  # only with voice separation


class BlurBody(BaseModel):
    enabled: bool
    x: int | None = None
    y: int | None = None
    width: int
    height: int
    strength: int
    tint: float
    font_size: int


# ----- data ----------------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/editor")
def get_editor(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
    job = manager.get(job_id)
    duration, _ = _duration(manager, job)
    output = _resolve_inside(manager, job, job.artifacts.get("output"))
    voice = _resolve_inside(manager, job, job.artifacts.get("voice_track"))
    return {
        "job": {
            "id": job.id,
            "status": job.status.value,
            "message": job.message,
            "current_stage": job.current_stage,
            "error": job.error,
            "input_name": job.input_path.name,
        },
        "duration": duration,
        "media": {
            "video": _resolve_inside(manager, job, job.input_path) is not None,
            "dub_video": output is not None,
            "voice_track": voice is not None,
        },
        "segments": [segment_json(manager, job, i) for i in range(len(job.segments))],
        "audio": _audio_settings(manager, job),
        "blur": _blur_settings(manager, job),
        "budget": None,  # kept for compatibility: there is no budget any more
    }


@router.patch("/api/jobs/{job_id}/segments/{index}")
def patch_segment(
    job_id: str, index: int, patch: SegmentPatch, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    with manager.editing(job_id) as job:
        seg = _segment(job, index)
        start = _finite(patch.start, "start time")
        end = _finite(patch.end, "end time")
        text = patch.text_vi.strip() if patch.text_vi is not None else None
        if start is None and end is None and text is None:
            raise InvalidEdit("Nothing to change: send start, end or text_vi.")
        if text is not None and not text:
            raise InvalidEdit("The Vietnamese text cannot be empty.")

        start_changed = start is not None and abs(start - seg.start) > 1e-9
        end_changed = end is not None and abs(end - seg.end) > 1e-9
        if start is not None or end is not None:
            new_start = seg.start if start is None else start
            new_end = seg.end if end is None else end
            _check_times(manager, job, index, new_start, new_end, start_changed, end_changed)
        else:
            new_start, new_end = seg.start, seg.end

        had_clip = _clip_path(manager, job, seg) is not None
        if text is not None and text != seg.text_vi.strip():
            seg.text_vi = text
            if had_clip:
                seg.audio_stale = True
        if start_changed or end_changed:
            seg.start, seg.end = new_start, new_end
            if had_clip and NEEDS_REALIGN not in seg.flags:
                seg.flags.append(NEEDS_REALIGN)
        return segment_json(manager, job, index)


def _check_times(
    manager: JobManager,
    job: Job,
    index: int,
    start: float,
    end: float,
    start_changed: bool,
    end_changed: bool,
) -> None:
    if start < 0:
        raise InvalidEdit("The start time cannot be negative.")
    if end - start < MIN_LINE_SECONDS - 1e-9:
        raise InvalidEdit(
            f"A line must start before it ends and last at least {MIN_LINE_SECONDS} seconds."
        )
    duration, known = _duration(manager, job)
    if known and end > duration + 0.001:
        raise InvalidEdit(
            f"The end time {format_time(end)} is after the end of the video "
            f"({format_time(duration)})."
        )
    if start_changed and index > 0:
        prev = job.segments[index - 1]
        if start < prev.end - OVERLAP_TOLERANCE:
            raise InvalidEdit(
                f"This line would start at {format_time(start)}, before the previous line "
                f"ends ({format_time(prev.end)}). Lines may overlap by at most "
                f"{OVERLAP_TOLERANCE} seconds."
            )
    if end_changed and index + 1 < len(job.segments):
        nxt = job.segments[index + 1]
        if end > nxt.start + OVERLAP_TOLERANCE:
            raise InvalidEdit(
                f"This line would end at {format_time(end)}, after the next line starts "
                f"({format_time(nxt.start)}). Lines may overlap by at most "
                f"{OVERLAP_TOLERANCE} seconds."
            )


# ----- voice ---------------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/segments/{index}/audio")
def segment_audio(
    job_id: str, index: int, manager: JobManager = Depends(get_manager)
) -> FileResponse:
    job = manager.get(job_id)
    path = _clip_path(manager, job, _segment(job, index))
    if path is None:
        raise JobNotFound("no voice yet")
    return FileResponse(path, headers=NO_CACHE)


@router.get("/api/jobs/{job_id}/segments/{index}/cost")
def segment_cost(
    job_id: str, index: int, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    """Kept for compatibility: voice generation is free and runs on this computer."""
    job = manager.get(job_id)
    seg = _segment(job, index)
    return {
        "chars": line_chars(seg),
        "remaining": None,
        "allowed": True,
        "message": "Free: runs on your computer; may take a few seconds.",
    }


@router.post("/api/jobs/{job_id}/segments/{index}/regenerate")
def regenerate(
    job_id: str,
    index: int,
    body: RegenerateBody | None = None,
    manager: JobManager = Depends(get_manager),
) -> dict[str, Any]:
    with manager.editing(job_id, save=False) as job:
        _segment(job, index)
        try:
            synthesize_line(job, index, manager.config, manager.tts_factory)
        except LineSynthError as exc:
            raise InvalidEdit(str(exc)) from exc
        except TTSVoiceError as exc:
            raise BadRequest(str(exc)) from exc
        except TTSSetupError as exc:
            raise UpstreamError(str(exc)) from exc
        except TTSError as exc:
            raise UpstreamError(f"The voice could not speak this line: {exc}") from exc
        except FFmpegError as exc:
            seg = job.segments[index]
            if NEEDS_REALIGN not in seg.flags:
                seg.flags.append(NEEDS_REALIGN)  # new clip exists; fit it later
            job.save()
            raise ServerProblem(
                "The new voice was made but could not be fitted to its slot; "
                "press Apply to try again."
            ) from exc
        job.save()
        return segment_json(manager, job, index)


@router.post("/api/jobs/{job_id}/segments/{index}/speed")
def set_speed(
    job_id: str, index: int, body: SpeedBody, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    speed = body.speed
    if speed is not None and not (math.isfinite(speed) and MIN_SPEED <= speed <= MAX_SPEED):
        raise InvalidEdit(f"The speed must be between {MIN_SPEED} and {MAX_SPEED}.")
    with manager.editing(job_id) as job:
        seg = _segment(job, index)
        if not has_clip(seg):
            raise JobStateError("This line has no voice yet; generate the voice first.")
        previous = seg.speed
        seg.speed = None if speed is None else round(speed, 3)
        try:
            fit_segment(job, index)
        except FFmpegError as exc:
            seg.speed = previous
            raise ServerProblem("The voice clip could not be changed.") from exc
        return segment_json(manager, job, index)


# ----- volumes and apply -------------------------------------------------------------------


@router.put("/api/jobs/{job_id}/audio-settings")
def put_audio_settings(
    job_id: str, body: AudioSettingsBody, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    for key, value in values.items():
        if not (math.isfinite(value) and 0.0 <= value <= MAX_VOLUME):
            raise InvalidEdit(f"{key.replace('_', ' ')} must be between 0 and {MAX_VOLUME:g}.")
    if values["ducked_volume"] > values["background_volume"] + 1e-9:
        raise InvalidEdit("The ducked volume cannot be louder than the background volume.")
    with manager.editing(job_id) as job:
        kept = {k: v for k, v in job.audio_overrides.items() if k not in values}
        job.audio_overrides = {**kept, **{k: float(v) for k, v in values.items()}}
        return _audio_settings(manager, job)


def _clean_blur(body: BlurBody, size: tuple[int, int] | None) -> dict[str, Any]:
    """Validated blur values; the box is kept inside the picture when its size is known."""
    values = body.model_dump()
    try:
        blur = BlurConfig.model_validate(values)
    except Exception as exc:
        errors = getattr(exc, "errors", None)
        detail = errors()[0] if errors else {}
        field = ".".join(str(p) for p in detail.get("loc", ())) or "a value"
        raise InvalidEdit(
            f"The blur setting {field} is not valid: {detail.get('msg', exc)}."
        ) from exc
    if size is not None:
        box: Box = resolve_box(blur, size[0], size[1])
        values.update(x=box.x, y=box.y, width=box.width, height=box.height)
    values["tint"] = round(float(values["tint"]), 2)
    return values


@router.put("/api/jobs/{job_id}/blur-settings")
def put_blur_settings(
    job_id: str, body: BlurBody, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    with manager.editing(job_id) as job:
        job.blur_overrides = _clean_blur(body, _video_size(manager, job))
        return _blur_settings(manager, job)


@router.delete("/api/jobs/{job_id}/blur-settings")
def reset_blur_settings(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
    """Forget this video's own blur choices: it follows the global default again."""
    with manager.editing(job_id) as job:
        job.blur_overrides = {}
        return _blur_settings(manager, job)


@router.put("/api/blur-defaults")
def put_blur_defaults(body: BlurBody, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
    """Save the values as the default for new videos (written to config/local.yaml)."""
    values = body.model_dump()
    values["tint"] = round(float(values["tint"]), 2)
    _clean_blur(body, None)  # validate first
    try:
        save_local_override("blur", values)
    except ConfigError as exc:
        raise ServerProblem(str(exc)) from exc
    manager.config.blur = BlurConfig.model_validate(values)
    return {"defaults": manager.config.blur.model_dump()}


@router.post("/api/jobs/{job_id}/apply")
def apply_changes(job_id: str, manager: JobManager = Depends(get_manager)) -> dict[str, Any]:
    missing: list[int] = []

    def prepare(job: Job) -> None:
        if not job.segments:
            raise JobStateError("There are no lines to apply yet.")
        audio = _resolve_inside(manager, job, job.artifacts.get("audio"))
        if audio is None:
            raise JobStateError("The original audio has not been extracted yet.")
        if _resolve_inside(manager, job, job.input_path) is None:
            raise JobStateError("The original video file is missing.")
        for i, seg in enumerate(job.segments):
            if (seg.text_zh.strip() or seg.text_vi.strip()) and not seg.text_vi.strip():
                raise InvalidEdit(f"Line {i + 1} has no Vietnamese text.")
        for i, seg in enumerate(job.segments):
            seg.aligned_path = None  # every line is fitted again with the current times/speed
            seg.flags = [f for f in seg.flags if f not in FIT_FLAGS]
            if seg.text_vi.strip() and not has_clip(seg):
                missing.append(i)

    manager.apply_edits(job_id, prepare)
    return {"started": True, "missing_audio": missing}


# ----- waveforms (timeline) ------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/waveforms")
def get_waveforms(
    job_id: str, index: int | None = None, manager: JobManager = Depends(get_manager)
) -> dict[str, Any]:
    """Loudness summaries for the timeline: one per voice clip, plus the original audio.

    With ``?index=N`` only that line's clip is returned (used after a clip was regenerated).
    Peaks are integers 0..100, ``*_rate`` per second; a clip's ``duration`` is in seconds.
    """
    job = manager.get(job_id)
    indexes = range(len(job.segments)) if index is None else [index]
    voice: dict[str, Any] = {}
    for i in indexes:
        path = _clip_path(manager, job, _segment(job, i))
        if path is None:
            continue
        try:
            duration, peaks = file_peaks(path, VOICE_PEAKS_PER_SECOND)
        except WaveformError:
            continue
        voice[str(i)] = {"duration": duration, "peaks": peaks}
    result: dict[str, Any] = {"voice_rate": VOICE_PEAKS_PER_SECOND, "voice": voice}
    if index is None:
        background = None
        stems = _stem_path(manager, job, "vocals") is not None
        parts = {
            k: _resolve_inside(manager, job, job.artifacts.get(k)) for k in ("vocals", "background")
        }
        stems = stems and all(parts.values())
        path = (
            parts["background"]
            if stems
            else _resolve_inside(manager, job, job.artifacts.get("audio"))
        )
        if stems:
            try:
                duration, peaks = file_peaks(parts["vocals"], BACKGROUND_PEAKS_PER_SECOND)
                result["voice_zh"] = {"duration": duration, "peaks": peaks}
            except WaveformError:
                stems = False
        result["stems"] = stems
        if path is not None:
            try:
                duration, peaks = file_peaks(path, BACKGROUND_PEAKS_PER_SECOND)
                background = {"duration": duration, "peaks": peaks}
            except WaveformError:
                background = None
        result["background_rate"] = BACKGROUND_PEAKS_PER_SECOND
        result["background"] = background
    return result


# ----- media ---------------------------------------------------------------------------------


def _media(manager: JobManager, job_id: str, kind: str) -> FileResponse:
    job = manager.get(job_id)
    if kind == "video":
        path = _resolve_inside(manager, job, job.input_path)
        missing = "The original video is not available."
    elif kind == "dub":
        path = _resolve_inside(manager, job, job.artifacts.get("output"))
        missing = "The dubbed video is not ready yet."
    else:
        path = _resolve_inside(manager, job, job.artifacts.get("voice_track"))
        missing = "The voice track is not ready yet."
    if path is None:
        raise JobNotFound(missing)
    return FileResponse(path, headers=MEDIA_HEADERS)


@router.get("/api/jobs/{job_id}/media/video")
def media_video(job_id: str, manager: JobManager = Depends(get_manager)) -> FileResponse:
    return _media(manager, job_id, "video")


@router.get("/api/jobs/{job_id}/media/dub")
def media_dub(job_id: str, manager: JobManager = Depends(get_manager)) -> FileResponse:
    return _media(manager, job_id, "dub")


@router.get("/api/jobs/{job_id}/media/stem/{kind}")
def media_stem(job_id: str, kind: str, manager: JobManager = Depends(get_manager)) -> FileResponse:
    """A separated part (``vocals`` = Chinese voice, ``background`` = music), small aac copy."""
    job = manager.get(job_id)
    path = _stem_path(manager, job, kind) if kind in ("vocals", "background") else None
    if path is None:
        raise JobNotFound("This video has no separated voice and music.")
    return FileResponse(path, headers=MEDIA_HEADERS)


@router.get("/api/jobs/{job_id}/media/voice")
def media_voice(job_id: str, manager: JobManager = Depends(get_manager)) -> FileResponse:
    return _media(manager, job_id, "voice")


__all__ = ["editor_page_html", "router"]
