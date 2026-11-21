"""FastAPI application factory for the local web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).parent


def create_app() -> FastAPI:
    app = FastAPI(title="video-translator")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static", check_dir=False), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    @app.post("/jobs", status_code=501)
    def create_job() -> None:
        """Placeholder: will accept an upload and start a pipeline run."""
        raise HTTPException(status_code=501, detail="Not implemented yet")

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> None:
        """Placeholder: will return the status and artifacts of a job."""
        raise HTTPException(status_code=501, detail="Not implemented yet")

    return app
