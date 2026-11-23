"""Job manager for the web UI: stores jobs on disk and runs the pipeline in a background thread.

Jobs live in ``config.paths.work/<job id>/job.json``. Only one job runs at a time; further
starts are queued and run in order. A job left in RUNNING state on disk by a previous
server process is shown as PAUSED with a hint to press Resume.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from video_translator.config import AppConfig
from video_translator.models import JOB_FILE_NAME, Job, JobStatus
from video_translator.paths import resolve_path, safe_name

if TYPE_CHECKING:
    from video_translator.pipeline import Pipeline
    from video_translator.synth_line import TTSFactory

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
ALLOWED_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".ts")
UPLOAD_CHUNK = 1024 * 1024  # 1 MB
INTERRUPTED_MESSAGE = (
    "This job was interrupted (the program was closed or crashed while it was running). "
    "Press Resume to continue; finished steps are not repeated."
)
QUEUED_MESSAGE = "Waiting for another job to finish."
# Statuses in which the editor may change a job (never while it is running or queued).
EDITABLE_STATUSES = (
    JobStatus.AWAITING_REVIEW,
    JobStatus.DONE,
    JobStatus.PAUSED,
    JobStatus.FAILED,
)
# Stages the editor's "apply" re-runs; synthesize and review never run again.
APPLY_STAGES = ("subtitles", "align", "mix", "render")

PipelineFactory = Callable[[AppConfig], "Pipeline"]


class JobError(Exception):
    """Base class for errors the web layer turns into HTTP responses."""

    status_code = 400


class InvalidJobId(JobError):
    status_code = 404


class JobNotFound(JobError):
    status_code = 404


class InvalidUpload(JobError):
    status_code = 400


class InvalidEdit(JobError):
    status_code = 422


class JobStateError(JobError):
    """The action is not possible in the job's current state."""

    status_code = 409


def default_pipeline_factory(config: AppConfig) -> Pipeline:
    from video_translator.pipeline import Pipeline
    from video_translator.stages import build_stages

    return Pipeline(build_stages(config), config)


def apply_pipeline_factory(config: AppConfig) -> Pipeline:
    """Pipeline for the editor's apply: subtitles, align, mix, render (no synthesize)."""
    from video_translator.pipeline import Pipeline
    from video_translator.stages import build_stages

    return Pipeline([st for st in build_stages(config) if st.name in APPLY_STAGES], config)


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise InvalidJobId("Unknown job.")
    return job_id


def clean_upload_name(filename: str | None) -> str:
    """Keep only a safe file name (no folders) with an allowed video extension."""
    base = (filename or "").replace("\\", "/").split("/")[-1].strip()
    name = safe_name(base) if base else ""
    suffix = Path(name).suffix.lower()
    if not name or suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(ext.lstrip(".") for ext in ALLOWED_EXTENSIONS)
        raise InvalidUpload(f"Unsupported file type. Please upload a video file ({allowed}).")
    return name


class JobManager:
    def _refresh_config(self) -> None:
        """Pick up edited settings; the folders (paths) stay as the server started with them."""
        if self.config_loader is None:
            return
        try:
            fresh = self.config_loader()
        except Exception as exc:  # a broken file: keep running with the settings we have
            log.warning("Could not re-read the settings, keeping the old ones: %s", exc)
            return
        self.config = fresh.model_copy(update={"paths": self.config.paths})

    def __init__(
        self,
        config: AppConfig,
        pipeline_factory: PipelineFactory | None = None,
        tts_factory: TTSFactory | None = None,
        config_loader: Callable[[], AppConfig] | None = None,
    ) -> None:
        self.config = config
        # Re-read the settings files whenever a job starts, so an edit of config/local.yaml
        # applies to the next run or resume without restarting the server.
        self.config_loader = config_loader
        self.pipeline_factory: PipelineFactory = pipeline_factory or default_pipeline_factory
        self.tts_factory = tts_factory  # None = the configured provider (editor: one line)
        self.apply_factory: PipelineFactory = apply_pipeline_factory
        self._lock = threading.RLock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._factory_overrides: dict[str, PipelineFactory] = {}
        self._live: dict[str, Job] = {}  # snapshots of queued/running jobs
        self._progress: dict[str, dict[str, Any]] = {}
        self._queued: list[str] = []
        self._running: str | None = None
        self._queue: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stage_total: int | None = None
        self._mark_interrupted_on_disk()

    # ----- folders -------------------------------------------------------------------------

    @property
    def work_root(self) -> Path:
        return resolve_path(self.config.paths.work)

    @property
    def input_root(self) -> Path:
        return resolve_path(self.config.paths.input)

    @property
    def output_root(self) -> Path:
        return resolve_path(self.config.paths.output)

    def _job_folder(self, job_id: str) -> Path:
        return self.work_root / check_job_id(job_id)

    # ----- reading -------------------------------------------------------------------------

    def _is_active(self, job_id: str) -> bool:
        return job_id == self._running or job_id in self._queued

    def _mark_interrupted_on_disk(self) -> None:
        """At start-up no job is running, so a RUNNING job on disk was interrupted."""
        for job in self._read_all_from_disk():
            if job.status == JobStatus.RUNNING:
                self._apply_interrupted(job)
                try:
                    job.save()
                except OSError:
                    log.warning("Could not update interrupted job %s", job.id)

    @staticmethod
    def _apply_interrupted(job: Job) -> None:
        job.status = JobStatus.PAUSED
        job.message = INTERRUPTED_MESSAGE

    def _read_from_disk(self, folder: Path) -> Job | None:
        if not JOB_ID_RE.fullmatch(folder.name):
            return None
        try:
            job = Job.load(folder)
        except (OSError, ValueError):
            return None
        if job.id != folder.name:
            return None
        return job

    def _read_all_from_disk(self) -> list[Job]:
        try:
            folders = [p for p in self.work_root.iterdir() if p.is_dir()]
        except OSError:
            return []
        jobs = [job for job in (self._read_from_disk(f) for f in folders) if job is not None]
        return jobs

    def _load(self, job_id: str) -> Job:
        """Current state of a job: the live copy if active, else fresh from disk."""
        check_job_id(job_id)
        with self._lock:
            live = self._live.get(job_id)
            if live is not None and self._is_active(job_id):
                return live.model_copy(deep=True)
        job = self._read_from_disk(self._job_folder(job_id))
        if job is None:
            raise JobNotFound("Job not found.")
        if job.status == JobStatus.RUNNING:
            self._apply_interrupted(job)
        return job

    def get(self, job_id: str) -> Job:
        return self._load(job_id)

    def _sort_key(self, job: Job) -> float:
        for path in (job.input_path, job.work_dir / JOB_FILE_NAME):
            try:
                return path.stat().st_mtime
            except OSError:
                continue
        return 0.0

    def list_jobs(self) -> list[Job]:
        """All jobs, newest first. Broken job folders are skipped."""
        by_id: dict[str, Job] = {job.id: job for job in self._read_all_from_disk()}
        with self._lock:
            for job_id, live in self._live.items():
                if self._is_active(job_id):
                    by_id[job_id] = live.model_copy(deep=True)
        jobs = list(by_id.values())
        for job in jobs:
            if job.status == JobStatus.RUNNING and not self._is_active(job.id):
                self._apply_interrupted(job)
        jobs.sort(key=self._sort_key, reverse=True)
        return jobs

    def is_queued(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._queued

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id == self._running

    def _total_stages(self) -> int | None:
        if self._stage_total is None:
            try:
                from video_translator.stages import V1_STAGES

                return len(V1_STAGES)
            except Exception:  # stages may be mid-edit or unavailable; progress is optional
                return None
        return self._stage_total

    def progress(self, job: Job) -> dict[str, Any]:
        """Progress info: current stage name, stages finished, total stages, fraction 0..1."""
        with self._lock:
            recorded = dict(self._progress.get(job.id, {}))
        total = recorded.get("total") or self._total_stages()
        if job.status == JobStatus.DONE:
            index = total or len(job.completed_stages)
            stage = ""
        else:
            index = recorded.get("index")
            if index is None or not self._is_active(job.id):
                index = len(job.completed_stages)
            stage = recorded.get("stage") or job.current_stage
            if not self._is_active(job.id):
                stage = job.current_stage
        fraction = min(1.0, index / total) if total else 0.0
        return {"stage": stage, "index": index, "total": total, "fraction": round(fraction, 3)}

    # ----- creating ------------------------------------------------------------------------

    def _unique_path(self, name: str) -> tuple[Path, BinaryIO]:
        """Open a new file in the input folder, adding ' (2)', ' (3)'... if the name is taken."""
        self.input_root.mkdir(parents=True, exist_ok=True)
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = name
        counter = 1
        while True:
            path = self.input_root / candidate
            try:
                return path, path.open("xb")
            except FileExistsError:
                counter += 1
                candidate = f"{stem} ({counter}){suffix}"

    def create_job(
        self, filename: str, fileobj: BinaryIO, separate_voice: bool | None = None
    ) -> Job:
        """Stream an uploaded video into the input folder and create a pending job for it.

        ``separate_voice`` None = the configured default (``separation.enabled``).
        """
        name = clean_upload_name(filename)
        path, out = self._unique_path(name)
        size = 0
        try:
            with out:
                while True:
                    chunk = fileobj.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    size += len(chunk)
            if size == 0:
                raise InvalidUpload("The uploaded file is empty.")
            job = Job.create(path, self.config.paths.work)
            job.separate_voice = (
                self.config.separation.enabled if separate_voice is None else separate_voice
            )
            job.save()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return job

    # ----- running -------------------------------------------------------------------------

    def start(self, job_id: str) -> Job:
        """Run a pending job (queued if another job is running)."""
        job = self._load(job_id)
        if job.status not in (JobStatus.PENDING, JobStatus.PAUSED, JobStatus.FAILED):
            raise JobStateError(f"This job cannot be started (it is {job.status.value}).")
        return self._enqueue(job, approve=False)

    def resume(self, job_id: str, approve: bool = False) -> Job:
        """Continue a paused, failed or review-pending job; ``approve`` approves the review."""
        job = self._load(job_id)
        if job.status == JobStatus.DONE:
            raise JobStateError("This job is already finished.")
        if approve and job.status != JobStatus.AWAITING_REVIEW:
            raise JobStateError("There is no translation waiting for approval.")
        if job.status == JobStatus.AWAITING_REVIEW and not approve:
            raise JobStateError("Approve the translation to continue.")
        return self._enqueue(job, approve=approve)

    def approve(self, job_id: str) -> Job:
        job = self._load(job_id)
        if job.status != JobStatus.AWAITING_REVIEW:
            raise JobStateError("There is no translation waiting for approval.")
        return self._enqueue(job, approve=True)

    def _enqueue(self, job: Job, approve: bool, factory: PipelineFactory | None = None) -> Job:
        with self._job_lock(job.id), self._lock:
            if self._is_active(job.id):
                raise JobStateError("This job is already running or waiting to run.")
            if approve:
                job.review_approved = True
            job.error = ""
            job.save()
            if self._running is not None:
                job.message = QUEUED_MESSAGE  # shown in the UI only, not written to disk
            if factory is not None:
                self._factory_overrides[job.id] = factory
            self._live[job.id] = job
            self._queued.append(job.id)
            self._queue.put((job.id, approve))
            self._ensure_worker()
            return job.model_copy(deep=True)

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, name="job-worker", daemon=True
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, _approve = item
            try:
                self._run_job(job_id)
            except Exception:  # never let the worker die
                log.exception("Unexpected error while running job %s", job_id)
                with self._lock:
                    self._finish(job_id)

    def _finish(self, job_id: str) -> None:
        """Called under the lock when a run ends: drop the live copy, free the worker slot."""
        if job_id in self._queued:
            self._queued.remove(job_id)
        if self._running == job_id:
            self._running = None
        self._live.pop(job_id, None)
        self._progress.pop(job_id, None)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            live = self._live.get(job_id)
            if live is None:
                return
            job = live.model_copy(deep=True)
            factory = self._factory_overrides.pop(job_id, None) or self.pipeline_factory
            self._queued.remove(job_id)
            self._running = job_id
            job.status = JobStatus.RUNNING
            job.message = ""
            self._live[job_id] = job.model_copy(deep=True)

        def on_progress(stage: str, completed: int, total: int) -> None:
            with self._lock:
                self._progress[job_id] = {"stage": stage, "index": completed, "total": total}
                self._stage_total = total
                snap = self._live.get(job_id)
                if snap is not None:
                    snap.current_stage = stage

        try:
            self._refresh_config()
            pipeline = factory(self.config)
            pipeline.resume(job, on_progress)
        except Exception as exc:  # the pipeline saves FAILED itself; this covers other errors
            log.warning("Job %s failed: %s", job_id, exc)
            on_disk = self._read_from_disk(self._job_folder(job_id))
            if on_disk is None or on_disk.status != JobStatus.FAILED:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                try:
                    job.save()
                except OSError:
                    log.warning("Could not save failed job %s", job_id)
        with self._lock:
            # Persisted state is on disk; the in-memory copy is no longer needed.
            self._finish(job_id)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until nothing is running or queued (used by tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._running is None and not self._queued:
                    return True
            time.sleep(0.01)
        return False

    def shutdown(self) -> None:
        self._queue.put(None)

    # ----- review --------------------------------------------------------------------------

    def save_edits(self, job_id: str, edits: dict[int, str]) -> Job:
        """Replace the Vietnamese text of some segments (only while awaiting review)."""
        with self._job_lock(job_id), self._lock:
            job = self._load(job_id)
            if job.status != JobStatus.AWAITING_REVIEW or self._is_active(job_id):
                raise JobStateError("The translation can only be edited while it awaits review.")
            if not edits:
                raise InvalidEdit("No edits were sent.")
            cleaned: dict[int, str] = {}
            for index, text in edits.items():
                if isinstance(index, bool) or not isinstance(index, int):
                    raise InvalidEdit(f"Invalid segment number: {index!r}.")
                if not 0 <= index < len(job.segments):
                    raise InvalidEdit(f"Segment {index} does not exist.")
                if not isinstance(text, str) or not text.strip():
                    raise InvalidEdit(f"The Vietnamese text of segment {index} cannot be empty.")
                cleaned[index] = text.strip()
            for index, text in cleaned.items():
                job.segments[index].text_vi = text
            job.save()
            return job

    # ----- editor --------------------------------------------------------------------------

    def _job_lock(self, job_id: str) -> threading.RLock:
        """Per-job lock: edits, regeneration and (re)starts of one job never interleave.

        Lock order is always job lock first, then ``self._lock``.
        """
        with self._lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = self._job_locks[job_id] = threading.RLock()
            return lock

    def _load_editable(self, job_id: str) -> Job:
        """The job from disk if the editor may change it now; 409 while running or queued."""
        check_job_id(job_id)
        with self._lock:
            if self._is_active(job_id):
                raise JobStateError(
                    "This video is being processed right now. Try again when it is done."
                )
            job = self._load(job_id)
            if job.status not in EDITABLE_STATUSES:
                raise JobStateError(
                    f"This video cannot be edited yet (it is {job.status.value.replace('_', ' ')})."
                )
            return job

    @contextmanager
    def editing(self, job_id: str, save: bool = True) -> Iterator[Job]:
        """Exclusive access to a job for an edit: yields the fresh job, saves it on success.

        Raises ``JobStateError`` (409) if the job is running, queued or in a state that
        cannot be edited.
        """
        with self._job_lock(job_id):
            job = self._load_editable(job_id)
            yield job
            if save:
                with self._lock:
                    if self._is_active(job_id):  # started meanwhile (should not happen)
                        raise JobStateError("This video started processing; the edit was dropped.")
                    job.save()

    def apply_edits(self, job_id: str, prepare: Callable[[Job], None]) -> Job:
        """Run ``prepare(job)`` on the editable job, save it and start the apply pipeline
        (subtitles, align, mix, render; never synthesize)."""
        with self._job_lock(job_id):
            job = self._load_editable(job_id)
            prepare(job)
            job.completed_stages = [
                name for name in job.completed_stages if name not in APPLY_STAGES
            ]
            job.status = JobStatus.PAUSED  # so the run starts from a resumable state
            job.message = ""
            return self._enqueue(job, approve=False, factory=self.apply_factory)
