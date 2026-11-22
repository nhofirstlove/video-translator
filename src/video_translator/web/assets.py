"""Static files that are never served from a stale browser cache.

A browser may keep ``editor.css`` for hours without asking the server again, so after an update
it shows the new page with the old styles. Two fixes work together: every ``/static/...`` link in
a page gets ``?v=<file version>`` (a new file version is a new address), and static files are sent
with ``Cache-Control: no-cache`` (the browser asks each time and gets a cheap "not modified").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from starlette.staticfiles import StaticFiles

_LINK = re.compile(r'((?:href|src)=")(/static/[^"?#]+)(")')


def add_versions(html: str, static_dir: Path) -> str:
    """``html`` with ``?v=<mtime>`` added to every ``/static/...`` link (unknown files kept)."""

    def fix(match: re.Match[str]) -> str:
        name = match.group(2)[len("/static/") :]
        try:
            stamp = (static_dir / name).stat().st_mtime_ns
        except OSError:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}?v={stamp // 1_000_000}{match.group(3)}"

    return _LINK.sub(fix, html)


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
