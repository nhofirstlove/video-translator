"""Command line interface: ``run``, ``resume``, ``serve`` and ``doctor``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from video_translator.config import AppConfig, ConfigError, load_config
from video_translator.models import Job, JobStatus
from video_translator.pipeline import Pipeline
from video_translator.stages import build_stages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-translator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="translate one video")
    run.add_argument("video", help="path to the input Chinese video")
    run.add_argument("--config", default=None, help="path to a YAML config file")

    resume = sub.add_parser("resume", help="continue a paused, reviewed or failed job")
    resume.add_argument("job_dir", help="the job folder printed by `run`")
    resume.add_argument("--config", default=None, help="path to a YAML config file")
    resume.add_argument(
        "--approve", action="store_true", help="approve the reviewed translation and continue"
    )

    sub.add_parser("doctor", help="check that this computer is ready (ffmpeg, keys, config)")

    serve = sub.add_parser("serve", help="start the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def _print_progress(stage: str, completed: int, total: int) -> None:
    print(f"[{completed}/{total}] {stage}", flush=True)


def _execute(job: Job, config: AppConfig) -> int:
    pipeline = Pipeline(build_stages(config), config)
    try:
        job = pipeline.resume(job, _print_progress)
    except Exception as exc:  # the pipeline already saved the FAILED status
        print(f"Failed in stage '{job.current_stage}': {exc}", file=sys.stderr)
        print(f"Fix the problem, then run: video-translator resume {job.work_dir}")
        return 1
    print(f"Status: {job.status.value}")
    if job.status in (JobStatus.AWAITING_REVIEW, JobStatus.PAUSED):
        if job.message:
            print(job.message)
        approve = " --approve" if job.status == JobStatus.AWAITING_REVIEW else ""
        print(f"Continue with: video-translator resume {job.work_dir}{approve}")
    elif job.status == JobStatus.DONE and "output" in job.artifacts:
        print(f"Output: {job.artifacts['output']}")
    return 0


def run_command(video: str, config_path: str | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    input_path = Path(video).expanduser()
    if not input_path.is_file():
        print(f"Input video not found: {input_path}", file=sys.stderr)
        return 1
    job = Job.create(input_path.resolve(), config.paths.work)
    print(f"Job folder: {job.work_dir}")
    return _execute(job, config)


def resume_command(job_dir: str, config_path: str | None, approve: bool) -> int:
    try:
        config = load_config(config_path)
        job = Job.load(Path(job_dir).expanduser())
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Cannot resume: {exc}", file=sys.stderr)
        return 1
    if approve:
        job.review_approved = True
    return _execute(job, config)


def main(argv: Sequence[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        from video_translator.doctor import main as doctor_main

        return doctor_main()
    if args.command == "run":
        return run_command(args.video, args.config)
    if args.command == "resume":
        return resume_command(args.job_dir, args.config, args.approve)
    if args.command == "serve":
        import uvicorn

        from video_translator.web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
