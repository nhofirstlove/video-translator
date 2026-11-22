"""Helpers for tests that drive the real web app in a real browser (Playwright + Chromium).

``browser_launch_kwargs()`` returns ``None`` when Playwright or a usable Chromium is missing, so
the browser tests are skipped (never failed) on machines without them. ``LiveServer`` runs the
real FastAPI app with uvicorn on a free port in a background thread.
"""

from __future__ import annotations

import glob
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

BROWSER_ARGS = ["--no-sandbox", "--autoplay-policy=no-user-gesture-required"]

# Records every media element that was asked to play, so a test can inspect what the page did.
TRACK_MEDIA_JS = """
(() => {
  window.__media = [];
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (!window.__media.includes(this)) window.__media.push(this);
    return play.apply(this, arguments);
  };
})();
"""


def _chromium_candidates() -> list[str | None]:
    """``None`` means "whatever Playwright finds itself"; then any Chromium we can locate."""
    found: list[str | None] = [None]
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers"]
    roots.append(str(Path.home() / ".cache" / "ms-playwright"))
    for root in filter(None, roots):
        found += sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"), reverse=True)
    return found


def browser_launch_kwargs() -> dict[str, Any] | None:
    """Launch options that work on this machine, or ``None`` if there is no usable browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    for candidate in _chromium_candidates():
        kwargs: dict[str, Any] = {"args": BROWSER_ARGS, "headless": True}
        if candidate:
            kwargs["executable_path"] = candidate
        try:
            with sync_playwright() as p:
                p.chromium.launch(**kwargs).close()
        except Exception:  # noqa: BLE001  (no browser here, wrong version, no sandbox, ...)
            continue
        return kwargs
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServer:
    """The real app served by uvicorn in a background thread (``url`` is its base URL)."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> LiveServer:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the test server did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
