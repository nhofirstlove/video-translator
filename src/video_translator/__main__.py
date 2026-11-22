"""Allow ``python -m video_translator ...`` (same as the ``video-translator`` command)."""

from __future__ import annotations

from video_translator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
