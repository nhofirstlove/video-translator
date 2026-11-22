"""VieNeu-TTS: free, local, open source (Apache-2.0) Vietnamese text to speech.

Two modes (``vieneu.mode`` in the config):

* ``library`` (default): the ``vieneu`` Python package (``pip install vieneu``) runs inside this
  program. The model is created ONCE per process on first use (the very first run downloads it
  from Hugging Face, so internet is needed once) and shared by every call; calls are serialized
  with a lock because thread-safety of one model instance is not documented.
* ``server``: a VieNeu server started separately (``python -m apps.openai_speech``, default
  http://localhost:8000) is called over HTTP at ``/v1/audio/speech``. Only the standard library
  is used. The server ignores ``speed``; timing is fitted later with ffmpeg.

Output is 48 kHz mono WAV in both modes. The ``vieneu`` package is imported lazily, never when
this module is imported, so tests and installs without it keep working.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from video_translator.config import AppConfig
from video_translator.providers.registry import register
from video_translator.providers.tts.base import (
    TTSError,
    TTSProvider,
    TTSSetupError,
    TTSVoiceError,
)

log = logging.getLogger(__name__)

AUDIO_EXTENSION = ".wav"
SERVER_MODEL = "vieneu-v3-turbo"
SERVER_PATH = "/v1/audio/speech"
INSTALL_HELP = (
    "The VieNeu-TTS voice package is not installed. Open a terminal, activate the project's "
    "virtual environment, then run: pip install vieneu"
)
MODEL_HELP = (
    "Could not load the VieNeu-TTS voice model. The first run downloads it from Hugging Face, "
    "so internet is needed once; check your connection and try again."
)
SERVER_HELP = (
    "Cannot reach the VieNeu server at {url}. Start it in its own terminal (in the VieNeu-TTS "
    "folder: uv run python -m apps.openai_speech), or set `vieneu: {{mode: library}}` in "
    "config/local.yaml to run the voice inside this program."
)

# ----- shared model (library mode) -----------------------------------------------------------

_MODEL_LOCK = threading.Lock()  # guards creating the model
_CALL_LOCK = threading.Lock()  # serializes use of the shared model
_MODELS: dict[str, Any] = {}  # precision -> loaded Vieneu instance


def _load_model(precision: str) -> Any:
    """The process-wide VieNeu model for ``precision``, created on first use."""
    with _MODEL_LOCK:
        model = _MODELS.get(precision)
        if model is not None:
            return model
        try:
            import vieneu  # lazy: the package is optional and heavy
        except ImportError as exc:
            raise TTSSetupError(INSTALL_HELP) from exc
        log.info(
            "Loading the VieNeu-TTS voice model (the first run downloads it; this can take "
            "a few minutes)..."
        )
        try:
            model = vieneu.Vieneu() if precision == "fp32" else vieneu.Vieneu(precision=precision)
        except Exception as exc:  # download, ONNX or memory problems
            raise TTSSetupError(f"{MODEL_HELP} Details: {exc}") from exc
        _MODELS[precision] = model
        return model


def reset_shared_model() -> None:
    """Forget the loaded models (tests)."""
    with _MODEL_LOCK:
        _MODELS.clear()


class _LibraryTransport:
    """Speaks with the ``vieneu`` Python package."""

    def __init__(self, precision: str = "fp32") -> None:
        self.precision = precision

    def list_voices(self) -> list[tuple[str, str]]:
        model = _load_model(self.precision)
        with _CALL_LOCK:
            try:
                return [(str(label), str(vid)) for label, vid in model.list_preset_voices()]
            except Exception:
                return []

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        model = _load_model(self.precision)
        with _CALL_LOCK:
            try:
                audio = model.infer(text, voice=voice)
                model.save(audio, str(out_path))
            except Exception as exc:
                raise TTSError(f"VieNeu-TTS could not speak this text: {exc}") from exc


class _ServerTransport:
    """Speaks with a VieNeu server over HTTP (standard library only)."""

    def __init__(self, server_url: str, timeout: float) -> None:
        self.url = server_url.rstrip("/")
        self.timeout = timeout

    def list_voices(self) -> list[tuple[str, str]]:
        return []

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        payload = json.dumps(
            {"model": SERVER_MODEL, "input": text, "voice": voice, "response_format": "wav"}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url + SERVER_PATH,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300].decode("utf-8", "replace")
            raise TTSError(
                f"The VieNeu server refused the request (HTTP {exc.code}): {body}"
            ) from exc
        except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TTSError(
                    f"The VieNeu server at {self.url} did not answer within "
                    f"{self.timeout:g} seconds."
                ) from exc
            raise TTSSetupError(SERVER_HELP.format(url=self.url)) from exc
        if not audio or audio.lstrip()[:1] == b"{":
            raise TTSError(
                "The VieNeu server returned no audio" + (f": {audio[:200]!r}" if audio else ".")
            )
        out_path.write_bytes(audio)


def _matches(voice: str, label: str, voice_id: str) -> bool:
    want = voice.strip().casefold()
    return want in (label.strip().casefold(), voice_id.strip().casefold()) or (
        label.strip().casefold().startswith(want + " (")
    )


@register("tts", "vieneu")
class VieNeuTTS(TTSProvider):
    """Speaks text with VieNeu-TTS; ``transport`` (an object with
    ``synthesize(text, voice, out_path)`` that writes an audio file, and ``list_voices()``
    returning ``(label, voice_id)`` pairs) can be injected for tests."""

    extension = AUDIO_EXTENSION

    def __init__(
        self,
        device: str = "cpu",
        *,
        config: AppConfig | None = None,
        transport: Any = None,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        self.config = (config or AppConfig()).vieneu
        self.voice = options.get("voice") or self.config.voice
        self._transport = transport

    def _get_transport(self) -> Any:
        if self._transport is None:
            if self.config.mode == "server":
                self._transport = _ServerTransport(
                    self.config.server_url, self.config.timeout_seconds
                )
            else:
                self._transport = _LibraryTransport(self.config.precision)
        return self._transport

    def _preset_pairs(self) -> list[tuple[str, str]]:
        try:
            return list(self._get_transport().list_voices())
        except TTSSetupError:
            raise
        except Exception:
            return []

    def list_voices(self) -> list[str]:
        """Names of the preset voices (library mode); empty in server mode."""
        return [label for label, _ in self._preset_pairs()]

    def _check_voice(self, voice: str) -> None:
        pairs = self._preset_pairs()
        if not pairs or any(_matches(voice, label, vid) for label, vid in pairs):
            return
        names = ", ".join(label for label, _ in pairs[:6])
        raise TTSVoiceError(
            f"Unknown VieNeu voice {voice!r}. Some valid voices: {names}. "
            "List them all with: video-translator voices"
        )

    def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> Path:
        text = (text or "").strip()
        if not text:
            raise TTSError("There is no text to speak.")
        voice = (voice or self.voice or "").strip() or self.config.voice
        transport = self._get_transport()
        self._check_voice(voice)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")
        try:
            transport.synthesize(text, voice, tmp)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise TTSError("VieNeu-TTS produced no audio.")
            os.replace(tmp, out_path)
        finally:
            tmp.unlink(missing_ok=True)
        return out_path
