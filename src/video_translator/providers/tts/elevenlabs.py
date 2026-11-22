"""ElevenLabs text to speech (official ``elevenlabs`` Python SDK, imported lazily).

Setup
-----
* API key: environment variable ``ELEVENLABS_API_KEY`` (never logged or written to files).
* Voice: set ``elevenlabs.voice`` (a voice id) in ``config/local.yaml``.
  To find a Vietnamese voice: open https://elevenlabs.io/app/voice-library, filter by
  Language = Vietnamese, press "Add to my voices" on one you like, then copy its voice id
  (the "ID" button next to the voice). ``ElevenLabsTTS.list_voices()`` lists voices of your
  account (best effort) so a UI can offer them.
* Model: ``eleven_multilingual_v2`` (supports Vietnamese), from ``config.elevenlabs.model``.

Output format: ``mp3_44100_128`` (the API default). It is allowed on the free tier; 192 kbps MP3
needs the Creator plan and 44.1 kHz PCM/WAV needs the Pro plan, so they are not used. ffmpeg
reads the mp3 files directly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from video_translator.config import AppConfig
from video_translator.providers.registry import register
from video_translator.providers.tts.base import TTSProvider

API_KEY_ENV = "ELEVENLABS_API_KEY"
OUTPUT_FORMAT = "mp3_44100_128"
AUDIO_EXTENSION = ".mp3"
MAX_ATTEMPTS = 5


class TTSError(RuntimeError):
    """A text to speech request failed."""


class TTSQuotaExceededError(TTSError):
    """The ElevenLabs character quota / credits are used up."""


class TTSConfigError(TTSError):
    """Missing voice id or API key."""


class TTSAPIError(TTSError):
    """HTTP-like failure from the service (a transport raises this to signal a status)."""

    def __init__(self, status_code: int | None, message: str = "") -> None:
        super().__init__(f"ElevenLabs error {status_code}: {message}")
        self.status_code = status_code
        self.body = message


_QUOTA_MARKERS = ("quota_exceeded", "quota exceeded", "insufficient", "credits", "character limit")


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _is_quota_error(exc: BaseException) -> bool:
    text = f"{getattr(exc, 'body', '')} {exc}".lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


class _SdkTransport:
    """Real transport: calls the ElevenLabs SDK (imported only when used)."""

    def __init__(self, api_key: str, output_format: str = OUTPUT_FORMAT) -> None:
        from elevenlabs.client import ElevenLabs  # lazy: keeps imports light and tests offline

        self._client = ElevenLabs(api_key=api_key)
        self._format = output_format

    def convert(self, text: str, voice_id: str, model_id: str) -> bytes:
        chunks = self._client.text_to_speech.convert(
            voice_id,
            text=text,
            model_id=model_id,
            output_format=self._format,
        )
        return b"".join(chunks)

    def list_voices(self) -> list[dict[str, str]]:
        response = self._client.voices.search(page_size=100)
        return [
            {"voice_id": v.voice_id, "name": v.name or "", "category": str(v.category or "")}
            for v in response.voices
        ]


@register("tts", "elevenlabs")
class ElevenLabsTTS(TTSProvider):
    """Speaks text with ElevenLabs; ``transport`` (an object with
    ``convert(text, voice_id, model_id) -> bytes``) can be injected for tests."""

    extension = AUDIO_EXTENSION

    def __init__(
        self,
        device: str = "cpu",
        *,
        config: AppConfig | None = None,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        cfg = (config or AppConfig()).elevenlabs
        self.voice = options.get("voice") or cfg.voice
        self.model = options.get("model") or cfg.model
        self._transport = transport
        self._sleep = sleep

    def _get_transport(self) -> Any:
        if self._transport is None:
            key = os.environ.get(API_KEY_ENV, "").strip()
            if not key:
                raise TTSConfigError(
                    f"No ElevenLabs API key. Set the environment variable {API_KEY_ENV}."
                )
            self._transport = _SdkTransport(key)
        return self._transport

    def _require_voice(self, voice: str | None) -> str:
        voice_id = (voice or self.voice or "").strip()
        if not voice_id:
            raise TTSConfigError(
                "No ElevenLabs voice chosen. Set `elevenlabs.voice` (a voice id) in "
                "config/local.yaml. Find a Vietnamese voice in the ElevenLabs voice library "
                "(Language: Vietnamese), add it to your voices and copy its ID."
            )
        return voice_id

    def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> Path:
        voice_id = self._require_voice(voice)
        transport = self._get_transport()
        out_path = Path(out_path)
        delay = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                audio = transport.convert(text, voice_id, self.model)
                break
            except (TTSQuotaExceededError, TTSConfigError):
                raise
            except Exception as exc:  # SDK ApiError or any transport error
                status = _status_of(exc)
                if _is_quota_error(exc) and status in (None, 401, 402, 429):
                    raise TTSQuotaExceededError(
                        "ElevenLabs character quota or credits used up."
                    ) from exc
                retryable = status == 429 or (status is not None and status >= 500)
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise TTSError(f"ElevenLabs request failed (status {status}): {exc}") from exc
                self._sleep(delay)
                delay = min(delay * 2, 30.0)
        if not audio:
            raise TTSError("ElevenLabs returned no audio.")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".part")
        tmp.write_bytes(audio)
        os.replace(tmp, out_path)
        return out_path

    def list_voices(self) -> list[dict[str, str]]:
        """Voices of the account as ``{"voice_id", "name", "category"}`` dicts; [] on failure."""
        try:
            transport = self._get_transport()
            lister = getattr(transport, "list_voices", None)
            return list(lister()) if lister else []
        except Exception:
            return []
