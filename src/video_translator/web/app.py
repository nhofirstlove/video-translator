"""FastAPI application factory for the local web UI.

Everything lives under ``/api``; ``/`` serves the single-page front end. The server is meant
for this computer only (the CLI binds to 127.0.0.1) and never returns API keys.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from video_translator.config import AppConfig, load_config
from video_translator.models import Job
from video_translator.web.jobs import (
    InvalidEdit,
    JobError,
    JobManager,
    JobNotFound,
    PipelineFactory,
)

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


def _characters_used(config: AppConfig) -> int | None:
    """ElevenLabs characters used this month (None if the counter cannot be read)."""
    try:
        from video_translator.usage import UsageTracker, default_usage_path

        tracker = UsageTracker(
            default_usage_path(config.paths.work), config.elevenlabs.monthly_character_budget
        )
        return tracker.used
    except Exception:  # usage tracking is optional; never break the page because of it
        return None


def create_app(
    config: AppConfig | None = None, pipeline_factory: PipelineFactory | None = None
) -> FastAPI:
    config = config if config is not None else load_config()
    manager = JobManager(config, pipeline_factory)

    app = FastAPI(title="video-translator")
    app.state.config = config
    app.state.jobs = manager
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static", check_dir=False), name="static")

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
            "downloads": {
                kind: _artifact_path(manager, job, kind) is not None for kind in DOWNLOADS
            },
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    # ----- jobs ----------------------------------------------------------------------------

    @app.post("/api/jobs")
    @app.post("/jobs", include_in_schema=False)
    def create_job(file: UploadFile = File(...), start: bool = Query(True)) -> dict[str, Any]:
        job = manager.create_job(file.filename or "", file.file)
        if start:
            manager.start(job.id)
        return job_json(manager.get(job.id))

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
            "gemini_model": config.gemini.model,
            "keys_set": {
                "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY", "").strip()),
            },
            "elevenlabs": {
                "monthly_character_budget": config.elevenlabs.monthly_character_budget,
                "characters_used_this_month": _characters_used(config),
            },
        }

    return app
