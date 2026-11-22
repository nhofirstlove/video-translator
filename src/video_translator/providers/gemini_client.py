"""Thin Gemini client (``google-genai`` SDK) with rate limiting, retries and quota detection.

The SDK is imported lazily, so importing this module needs neither the SDK nor the network.
Tests pass ``transport=FakeGeminiTransport([...])`` and never touch the network.
The API key is read from the environment (``GEMINI_API_KEY``) and is never logged or printed.
"""

from __future__ import annotations

import mimetypes
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

API_KEY_ENV_VAR = "GEMINI_API_KEY"
_DAILY_MARKERS = ("perday", "per day", "daily", "requests_per_day", "per_day")


class GeminiError(RuntimeError):
    """A Gemini call failed and will not be retried."""


class QuotaExhaustedError(GeminiError):
    """The DAILY quota is used up; stages turn this into a pause (resume tomorrow)."""


class GeminiAPIError(GeminiError):
    """An HTTP-style API error (``code`` is the HTTP status). Also handy for test fakes."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(f"Gemini API error {code}: {message}")
        self.code = code
        self.message = message


class GeminiTransport(Protocol):
    def generate_text(
        self, prompt: str, *, audio_path: Path | None = None, json_mode: bool = False
    ) -> str: ...


def _error_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _error_text(exc: BaseException) -> str:
    return f"{getattr(exc, 'message', '')} {getattr(exc, 'status', '')} {exc}".lower()


def is_daily_quota_error(exc: BaseException) -> bool:
    """True if ``exc`` is a 429 that says the per-DAY quota is used up."""
    if _error_code(exc) != 429:
        return False
    text = _error_text(exc)
    return any(marker in text for marker in _DAILY_MARKERS)


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth retrying: 429 per-minute limits, 5xx, network problems."""
    code = _error_code(exc)
    if code is not None:
        return code == 429 or 500 <= code < 600
    return isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__module__.startswith(
        ("httpx", "httpcore", "requests")
    )


class _SdkTransport:
    """The real thing: calls Gemini through the ``google-genai`` SDK."""

    def __init__(self, api_key: str | None, model: str) -> None:
        self._api_key = api_key
        self._model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            key = self._api_key or os.environ.get(API_KEY_ENV_VAR)
            if not key:
                raise GeminiError(
                    f"{API_KEY_ENV_VAR} is not set. Put it in .env "
                    "or run `video-translator doctor`."
                )
            from google import genai  # lazy: only needed for real calls

            self._client = genai.Client(api_key=key)
        return self._client

    def generate_text(
        self, prompt: str, *, audio_path: Path | None = None, json_mode: bool = False
    ) -> str:
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            response_mime_type="application/json" if json_mode else None
        )
        uploaded = None
        try:
            contents: list[Any] = []
            if audio_path is not None:
                mime = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
                uploaded = client.files.upload(
                    file=str(audio_path), config=types.UploadFileConfig(mime_type=mime)
                )
                contents.append(uploaded)
            contents.append(prompt)
            response = client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        finally:
            if uploaded is not None:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:  # noqa: BLE001 - cleanup is best effort (files expire anyway)
                    pass
        text = getattr(response, "text", None)
        if not text:
            raise GeminiError("Gemini returned no text (the response may have been blocked).")
        return text


class GeminiClient:
    """Rate-limited, retrying wrapper around a Gemini transport."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-3.6-flash",
        requests_per_minute: int = 10,
        transport: GeminiTransport | None = None,
        *,
        max_retries: int = 5,
        base_delay: float = 5.0,
        max_delay: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.model = model
        self.requests_per_minute = requests_per_minute
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()  # start times of recent calls (sliding 60 s window)
        self._transport: GeminiTransport = transport or _SdkTransport(api_key, model)

    def _wait_for_slot(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        if len(self._calls) >= self.requests_per_minute:
            self._sleep(max(0.0, 60.0 - (now - self._calls[0])))
            now = self._clock()
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
        self._calls.append(now)

    def generate_text(
        self, prompt: str, *, audio_path: Path | None = None, json_mode: bool = False
    ) -> str:
        """Send ``prompt`` (plus optional audio) and return the response text."""
        attempt = 0
        while True:
            self._wait_for_slot()
            try:
                return self._transport.generate_text(
                    prompt, audio_path=audio_path, json_mode=json_mode
                )
            except QuotaExhaustedError:
                raise
            except Exception as exc:
                if is_daily_quota_error(exc):
                    raise QuotaExhaustedError(
                        "Gemini daily quota is used up. Resume later (it resets daily)."
                    ) from exc
                if not is_transient_error(exc):
                    if isinstance(exc, GeminiError):
                        raise
                    raise GeminiError(f"Gemini call failed: {type(exc).__name__}: {exc}") from exc
                if attempt >= self.max_retries:
                    raise GeminiError(
                        f"Gemini call failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleep(min(self.max_delay, self.base_delay * (2**attempt)))
                attempt += 1


class FakeGeminiTransport:
    """Test double: returns queued canned responses and records what it was asked.

    Queue items are strings (returned) or exceptions (raised). ``prompts`` holds every prompt,
    ``calls`` holds dicts with ``prompt``, ``audio_path`` and ``json_mode``.
    """

    def __init__(self, responses: list[str | BaseException] | None = None) -> None:
        self.responses: list[str | BaseException] = list(responses or [])
        self.prompts: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def queue(self, *responses: str | BaseException) -> None:
        self.responses.extend(responses)

    def generate_text(
        self, prompt: str, *, audio_path: Path | None = None, json_mode: bool = False
    ) -> str:
        self.prompts.append(prompt)
        self.calls.append({"prompt": prompt, "audio_path": audio_path, "json_mode": json_mode})
        if not self.responses:
            raise GeminiError("FakeGeminiTransport has no queued responses left.")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
