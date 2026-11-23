"""Command line interface: ``run``, ``resume``, ``serve``, ``doctor``, ``voices`` and ``say``."""

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
    run.add_argument(
        "--separate-voice",
        action="store_true",
        help="split the voice from the music first (Demucs; slow on a CPU) for better timing",
    )

    resume = sub.add_parser("resume", help="continue a paused, reviewed or failed job")
    resume.add_argument("job_dir", help="the job folder printed by `run`")
    resume.add_argument("--config", default=None, help="path to a YAML config file")
    resume.add_argument(
        "--approve", action="store_true", help="approve the reviewed translation and continue"
    )

    sub.add_parser("doctor", help="check that this computer is ready (ffmpeg, keys, voice, config)")

    sub.add_parser("voices", help="list the preset VieNeu voices")

    say = sub.add_parser("say", help="speak one sample sentence with the configured voice")
    say.add_argument("text", help="Vietnamese text to speak")
    say.add_argument("--voice", default=None, help="voice name (see `video-translator voices`)")
    say.add_argument("--out", default="say.wav", help="output audio file (default: say.wav)")
    say.add_argument("--config", default=None, help="path to a YAML config file")

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


def run_command(video: str, config_path: str | None, separate_voice: bool = False) -> int:
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
    job.separate_voice = separate_voice or config.separation.enabled
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


def voices_command(config_path: str | None = None) -> int:
    from video_translator import synth_line
    from video_translator.providers.tts.base import TTSError

    try:
        config = load_config(config_path)
        provider = synth_line.default_tts_factory(config)
        if config.vieneu.mode == "server":
            print(
                "Server mode: the voices are chosen by the VieNeu server, so they cannot be "
                "listed here. Set `vieneu.voice` in config/local.yaml (for example 'Mai Anh')."
            )
            return 0
        choose = getattr(provider, "list_voice_choices", None)
        choices = choose() if choose else [(n, n) for n in provider.list_voices()]
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except TTSError as exc:
        print(f"Cannot list the voices: {exc}", file=sys.stderr)
        return 1
    if not choices:
        print("No preset voices were reported.")
        return 0
    print('Preset voices (set one with `vieneu: {voice: "NAME"}` in config/local.yaml):')
    for label, voice_id in choices:
        print(f"  {voice_id}" if label == voice_id else f"  {voice_id}   ({label})")
    return 0


def say_command(text: str, voice: str | None, out: str, config_path: str | None = None) -> int:
    from video_translator import synth_line
    from video_translator.providers.tts.base import TTSError

    try:
        config = load_config(config_path)
        provider = synth_line.default_tts_factory(config)
        path = provider.synthesize(text, Path(out).expanduser(), voice)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except TTSError as exc:
        print(f"Could not speak the text: {exc}", file=sys.stderr)
        return 1
    print(f"Saved: {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        from video_translator.doctor import main as doctor_main

        return doctor_main()
    if args.command == "voices":
        return voices_command()
    if args.command == "say":
        return say_command(args.text, args.voice, args.out, args.config)
    if args.command == "run":
        return run_command(args.video, args.config, args.separate_voice)
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
