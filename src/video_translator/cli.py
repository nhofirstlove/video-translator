"""Command line interface: ``video-translator run <video>`` and ``video-translator serve``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-translator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="translate one video (not implemented yet)")
    run.add_argument("video", help="path to the input Chinese video")
    run.add_argument("--config", default=None, help="path to a YAML config file")

    serve = sub.add_parser("serve", help="start the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        # Placeholder: will load config, build a Job and run the Pipeline.
        print(f"'run' is not implemented yet (video: {args.video}).")
        return 1
    if args.command == "serve":
        import uvicorn

        from video_translator.web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
