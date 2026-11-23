"""Translation through 9Router (free models first), with Gemini as the automatic last resort.

9Router is a small program that runs on this computer (default ``http://localhost:20128/v1``) and
speaks the OpenAI chat format. In its dashboard you connect free services and can build a
"combo": an ordered list of models it tries by itself. ``ninerouter.models`` lists the model or
combo names to use here, in order. For every batch of lines:

1. each listed model is tried in turn (an unreadable answer is asked for once more); a model that
   is out of quota, refused or unreachable is skipped for a while;
2. only when every listed model failed does Gemini translate that batch (the same prompt).

If Gemini fails as well the error says what happened to each step, and a Gemini daily-quota
problem pauses the job like before. The job message tells how many lines each one translated.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from video_translator.models import Segment
from video_translator.providers.gemini_client import QuotaExhaustedError
from video_translator.providers.registry import register
from video_translator.providers.translation.base import TranslationProvider
from video_translator.providers.translation.gemini import (
    CONTEXT_SIZE,
    GeminiTranslation,
    TranslationError,
    parse_response,
)

if TYPE_CHECKING:
    from video_translator.config import AppConfig, NineRouterConfig

PLACEHOLDER_KEY = "sk_9router"  # 9Router does not check the key unless REQUIRE_API_KEY is on
COOLDOWN_QUOTA = 900.0  # a model that is out of quota/credits rests this long (seconds)
COOLDOWN_REFUSED = 86400.0  # not allowed / not found: not this run
COOLDOWN_BAD = 120.0  # a model that answered badly or not at all
_CJK = re.compile(r"[㐀-鿿]")
_LETTER = re.compile(r"\w", re.UNICODE)


class NineRouterError(RuntimeError):
    """A call to 9Router failed. ``code`` is the HTTP status, or None for network problems."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class NineRouterTransport(Protocol):
    def chat(self, model: str, prompt: str) -> str: ...


class _HttpTransport:
    """POSTs ``/chat/completions`` (OpenAI format) to 9Router with the standard library."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._timeout = timeout

    def chat(self, model: str, prompt: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
            raise NineRouterError(f"9Router answered {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise NineRouterError(f"9Router is not reachable: {exc}") from exc
        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise NineRouterError("9Router sent an answer without any text") from exc
        if not isinstance(text, str) or not text.strip():
            raise NineRouterError("9Router sent an empty answer")
        return text


def check_vietnamese(zh: Sequence[str], vi: Sequence[str]) -> None:
    """Raise TranslationError if a "translation" is really still Chinese (a weak free model)."""
    for source, text in zip(zh, vi, strict=True):
        letters = _LETTER.findall(text)
        cjk = _CJK.findall(text)
        if letters and len(cjk) / len(letters) > 0.3:
            raise TranslationError("a line was left in Chinese")
        if source.strip() and text.strip() == source.strip():
            raise TranslationError("a line was copied instead of translated")


@register("translation", "ninerouter")
class NineRouterTranslation(TranslationProvider):
    """Free models through 9Router first; Gemini only when all of them failed for a batch."""

    def __init__(
        self,
        transport: NineRouterTransport | None = None,
        fallback: GeminiTranslation | None = None,
        config: AppConfig | None = None,
        settings: NineRouterConfig | None = None,
        models: Sequence[str] | None = None,
        batch_size: int | None = None,
        context_size: int = CONTEXT_SIZE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        if config is None and settings is None:
            from video_translator.config import load_config

            config = load_config()
        if settings is None:
            assert config is not None
            settings = config.ninerouter
        self.settings = settings
        self.models = list(models if models is not None else settings.models)
        self.batch_size = batch_size or settings.batch_size
        self.context_size = context_size
        self._config = config
        self._transport = transport
        self._fallback = fallback
        self._clock = clock
        self._sleep = sleep
        self._calls: list[float] = []
        self._rest_until: dict[str, float] = {}
        self._why: dict[str, str] = {}
        self.lines_by: dict[str, int] = {}  # who translated how many lines

    # --- helpers -------------------------------------------------------------------------

    @property
    def transport(self) -> NineRouterTransport:
        if self._transport is None:
            key = os.environ.get(self.settings.api_key_env, "").strip() or PLACEHOLDER_KEY
            self._transport = _HttpTransport(
                self.settings.base_url, key, self.settings.timeout_seconds
            )
        return self._transport

    @property
    def fallback(self) -> GeminiTranslation:
        if self._fallback is None:
            self._fallback = GeminiTranslation(config=self._config)
        return self._fallback

    def _wait_for_slot(self) -> None:
        now = self._clock()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= self.settings.requests_per_minute:
            self._sleep(max(0.0, 60.0 - (now - self._calls[0])))
            now = self._clock()
            self._calls = [t for t in self._calls if now - t < 60.0]
        self._calls.append(now)

    def _rest(self, model: str, seconds: float, why: str) -> None:
        self._rest_until[model] = self._clock() + seconds
        self._why[model] = why

    def _ready(self, model: str) -> bool:
        return self._clock() >= self._rest_until.get(model, 0.0)

    @property
    def notes(self) -> list[str]:
        parts = [f"{n} lines by {who}" for who, n in self.lines_by.items() if n]
        out = ["translated: " + ", ".join(parts)] if parts else []
        gemini = self.lines_by.get("Gemini", 0)
        if gemini:
            out.append(f"Gemini took over {gemini} line(s) because the free models failed")
        return out

    # --- translation ---------------------------------------------------------------------

    def translate(self, segments: list[Segment]) -> list[Segment]:
        result = [seg.model_copy(deep=True) for seg in segments]
        for start in range(0, len(result), self.batch_size):
            batch = result[start : start + self.batch_size]
            context = result[max(0, start - self.context_size) : start]
            for seg, vi in zip(batch, self.translate_batch(batch, context), strict=True):
                seg.text_vi = vi
        return result

    def translate_batch(
        self, batch: Sequence[Segment], context: Sequence[Segment] = ()
    ) -> list[str]:
        if not batch:
            return []
        prompt = self.fallback._build_prompt(batch, context)  # the same prompt for every model
        ids = list(range(len(batch)))
        zh = [s.text_zh for s in batch]
        for model in self.models:
            if not self._ready(model):
                continue
            for _attempt in range(2):
                self._wait_for_slot()
                try:
                    text = self.transport.chat(model, prompt)
                except NineRouterError as exc:
                    self._on_call_error(model, exc)
                    break
                try:
                    parsed = parse_response(text, ids)
                    translations = [parsed[i] for i in ids]
                    check_vietnamese(zh, translations)
                except TranslationError as exc:
                    self._why[model] = f"unusable answer ({exc})"
                    continue
                self.lines_by[model] = self.lines_by.get(model, 0) + len(batch)
                return translations
            else:
                self._rest(model, COOLDOWN_BAD, self._why.get(model, "unusable answers"))
        return self._use_gemini(batch, context)

    def _on_call_error(self, model: str, exc: NineRouterError) -> None:
        code = exc.code
        if code in (402, 429):
            self._rest(model, COOLDOWN_QUOTA, f"out of quota or rate limited ({code})")
        elif code in (400, 401, 403, 404):
            self._rest(model, COOLDOWN_REFUSED, f"refused or unknown ({code}): {exc}")
        else:
            self._rest(model, COOLDOWN_BAD, str(exc))

    def _use_gemini(self, batch: Sequence[Segment], context: Sequence[Segment]) -> list[str]:
        reasons = "; ".join(f"{m}: {self._why.get(m, 'resting')}" for m in self.models) or "none"
        try:
            translations = self.fallback.translate_batch(batch, context)
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            raise TranslationError(
                f"No free model in 9Router could translate this batch ({reasons}), and the "
                f"Gemini fallback failed too: {exc}"
            ) from exc
        self.lines_by["Gemini"] = self.lines_by.get("Gemini", 0) + len(batch)
        return translations
