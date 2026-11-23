"""FastAPI application factory for the local web UI.

Everything lives under ``/api``; ``/`` serves the single-page front end. The server is meant
for this computer only (the CLI binds to 127.0.0.1) and never returns API keys.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from video_translator.config import AppConfig, ConfigError, load_config, save_local_override
from video_translator.models import Job
from video_translator.synth_line import TTSFactory
from video_translator.web import editor_api
from video_translator.web.assets import NoCacheStaticFiles, add_versions
from video_translator.web.jobs import (
    InvalidEdit,
    JobError,
    JobManager,
    JobNotFound,
    PipelineFactory,
    check_job_id,
)
from video_translator.web.voice_samples import VoiceSamples, clean_voice

WEB_DIR = Path(__file__).parent

# Download kind -> artifact keys to try, in order, plus the label used in the file name.
DOWNLOADS: dict[str, tuple[tuple[str, ...], str]] = {
    "video": (("output",), "mp4"),
    "srt": (("srt_output", "srt"), "srt"),
}


class EditItem(BaseModel):
    index: int
    text_vi: str


class EditsBody(BaseModel):
    """Either ``{"edits": [{"index": 0, "text_vi": "..."}]}`` or ``{"edits": {"0": "..."}}``."""

    edits: list[EditItem] | dict[str, str]


class SampleBody(BaseModel):
    voice: str | None = None  # None = the configured voice
    text: str | None = None  # None or empty = the standard sample sentence


def _parse_edits(body: EditsBody) -> dict[int, str]:
    edits: dict[int, str] = {}
    if isinstance(body.edits, dict):
        for key, text in body.edits.items():
            try:
                index = int(key)
            except ValueError:
                raise InvalidEdit(f"Invalid segment number: {key!r}.") from None
            edits[index] = text
    else:
        for item in body.edits:
            if item.index in edits:
                raise InvalidEdit(f"Segment {item.index} appears twice.")
            edits[item.index] = item.text_vi
    return edits


def _artifact_path(manager: JobManager, job: Job, kind: str) -> Path | None:
    """The existing file for a download kind, only if it lies inside the work/output folders."""
    keys, _ = DOWNLOADS[kind]
    allowed = [manager.work_root.resolve(), manager.output_root.resolve()]
    for key in keys:
        raw = job.artifacts.get(key)
        if raw is None:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = job.work_dir / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file() and any(resolved.is_relative_to(root) for root in allowed):
            return resolved
    return None


def create_app(
    config: AppConfig | None = None,
    pipeline_factory: PipelineFactory | None = None,
    tts_factory: TTSFactory | None = None,
) -> FastAPI:
    reload_settings = config is None  # settings loaded from the files can be re-read per job
    config = config if config is not None else load_config()
    manager = JobManager(
        config,
        pipeline_factory,
        tts_factory,
        config_loader=load_config if reload_settings else None,
    )

    app = FastAPI(title="video-translator")
    app.state.config = config
    app.state.jobs = manager
    app.mount(
        "/static", NoCacheStaticFiles(directory=WEB_DIR / "static", check_dir=False), name="static"
    )

    @app.exception_handler(JobError)
    async def job_error_handler(request: Request, exc: JobError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    def job_json(job: Job) -> dict[str, Any]:
        queued = manager.is_queued(job.id)
        running = manager.is_running(job.id)
        progress = manager.progress(job)
        message = job.message
        if running and not message:
            stage = progress["stage"]
            message = f"Working on: {stage}" if stage else "Starting..."
        return {
            "id": job.id,
            "filename": job.input_path.name,
            "status": job.status.value,
            "queued": queued,
            "running": running,
            "message": message,
            "error": job.error,
            "current_stage": progress["stage"],
            "progress": progress,
            "segments": len(job.segments),
            "review_approved": job.review_approved,
            "separate_voice": job.separate_voice,
            "voice": job.voice or manager.config.vieneu.voice,
            "downloads": {
                kind: _artifact_path(manager, job, kind) is not None for kind in DOWNLOADS
            },
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)  # no icon: keeps the browser console free of 404s

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        return add_versions(page, WEB_DIR / "static")

    @app.get("/editor/{job_id}", response_class=HTMLResponse)
    def editor_page(job_id: str) -> str:
        check_job_id(job_id)
        return editor_api.editor_page_html(WEB_DIR)

    # Added as plain routes (not include_router) so ``app.routes`` lists every path.
    app.router.routes.extend(editor_api.router.routes)

    # ----- jobs ----------------------------------------------------------------------------

    @app.post("/api/jobs")
    @app.post("/jobs", include_in_schema=False)
    def create_job(
        file: UploadFile = File(...),
        start: bool = Query(True),
        separate_voice: bool | None = Query(None),
        voice: str | None = Query(None),
        make_default: bool = Query(False),
    ) -> dict[str, Any]:
        voice = clean_voice(voice) or None
        job = manager.create_job(file.filename or "", file.file, separate_voice, voice)
        extra: dict[str, Any] = {}
        if voice and make_default:
            try:
                save_local_override("vieneu", {"voice": voice})
                manager.reload_settings()
                extra["default_voice_saved"] = True
            except ConfigError as exc:  # the upload still works; say why it was not saved
                extra["default_voice_saved"] = False
                extra["default_voice_error"] = str(exc)
        if start:
            manager.start(job.id)
        return {**job_json(manager.get(job.id)), **extra}

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [job_json(job) for job in manager.list_jobs()]

    @app.get("/api/jobs/{job_id}")
    @app.get("/jobs/{job_id}", include_in_schema=False)
    def get_job(job_id: str) -> dict[str, Any]:
        return job_json(manager.get(job_id))

    @app.get("/api/jobs/{job_id}/segments")
    def get_segments(job_id: str) -> list[dict[str, Any]]:
        job = manager.get(job_id)
        return [
            {
                "index": i,
                "start": seg.start,
                "end": seg.end,
                "text_zh": seg.text_zh,
                "text_vi": seg.text_vi,
                "flags": seg.flags,
            }
            for i, seg in enumerate(job.segments)
        ]

    @app.put("/api/jobs/{job_id}/segments")
    def put_segments(job_id: str, body: EditsBody) -> dict[str, Any]:
        job = manager.save_edits(job_id, _parse_edits(body))
        return job_json(job)

    @app.post("/api/jobs/{job_id}/approve")
    def approve_job(job_id: str) -> dict[str, Any]:
        manager.approve(job_id)
        return job_json(manager.get(job_id))

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        manager.resume(job_id)
        return job_json(manager.get(job_id))

    @app.get("/api/jobs/{job_id}/download/{kind}")
    def download(job_id: str, kind: str) -> FileResponse:
        if kind not in DOWNLOADS:
            raise JobNotFound("Unknown download type.")
        job = manager.get(job_id)
        path = _artifact_path(manager, job, kind)
        if path is None:
            raise JobNotFound("This file is not ready yet.")
        stem = Path(job.input_path.name).stem or "video"
        suffix = path.suffix or f".{DOWNLOADS[kind][1]}"
        return FileResponse(path, filename=f"{stem}_vi{suffix}")

    # ----- voice picker (upload page) ------------------------------------------------------

    samples = VoiceSamples(manager)

    @app.get("/api/voices")
    def list_voices() -> dict[str, Any]:
        return samples.listing()

    @app.post("/api/voices/sample")
    def voice_sample(body: SampleBody) -> FileResponse:
        path = samples.sample(body.voice, body.text)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # ----- setup and settings --------------------------------------------------------------

    @app.get("/api/doctor")
    def doctor_endpoint() -> dict[str, Any]:
        from video_translator import doctor

        results = doctor.run_checks()
        return {
            "ok": doctor.exit_code(results) == 0,
            "checks": [{"name": r.name, "status": r.status, "message": r.message} for r in results],
        }

    @app.get("/api/config/summary")
    def config_summary() -> dict[str, Any]:
        return {
            "review": config.review,
            "providers": config.providers.model_dump(),
            "chunk_minutes": config.chunk_minutes,
            "separate_voice": manager.config.separation.enabled,
            "gemini_model": config.gemini.model,
            "keys_set": {
                "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            },
            "vieneu": {
                "mode": manager.config.vieneu.mode,
                "voice": manager.config.vieneu.voice,
            },
            "budget": None,  # kept for compatibility: voice generation is free
        }

    return app
