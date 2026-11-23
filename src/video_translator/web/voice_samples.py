"""The voice picker of the upload page: the list of voices and a short spoken sample of each.

Samples are made with the same voice engine as the dub (VieNeu on this computer), so what you
hear is what the video will sound like. Every sample is kept on disk under its voice and text,
so hearing the same voice and sentence again is instant. Nothing here touches a job.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from video_translator.providers.tts.base import (
    TTSError,
    TTSProvider,
    TTSSetupError,
    TTSVoiceError,
)
from video_translator.synth_line import TTSFactory, default_tts_factory
from video_translator.web.editor_api import BadRequest, UpstreamError
from video_translator.web.jobs import JobManager

DEFAULT_SAMPLE_TEXT = "Xin chào, đây là giọng đọc của video này. Bạn thấy giọng nói này thế nào?"
MAX_TEXT_CHARS = 300
MAX_VOICE_CHARS = 80
CACHE_DIR = "_voice_samples"


def clean_voice(voice: str | None) -> str:
    """The voice name as typed, trimmed; an invalid one raises ``BadRequest``."""
    voice = (voice or "").strip()
    if len(voice) > MAX_VOICE_CHARS or any(ord(ch) < 32 for ch in voice):
        raise BadRequest("That is not a valid voice name.")
    return voice


class VoiceSamples:
    """One voice engine kept alive for the picker (loading it takes a while), one sample at a
    time."""

    def __init__(self, manager: JobManager, tts_factory: TTSFactory | None = None) -> None:
        self.manager = manager
        self._factory = tts_factory or manager.tts_factory or default_tts_factory
        self._provider: TTSProvider | None = None
        self._lock = threading.Lock()

    def _get_provider(self) -> TTSProvider:
        if self._provider is None:
            self._provider = self._factory(self.manager.config)
        return self._provider

    def listing(self) -> dict[str, Any]:
        """Voices to choose from. When they cannot be listed (server mode, or the voice package
        is missing) ``voices`` is empty and ``note`` tells why; the page then lets the user type
        a name."""
        config = self.manager.config
        out: dict[str, Any] = {
            "mode": config.vieneu.mode,
            "default": config.vieneu.voice,
            "sample_text": DEFAULT_SAMPLE_TEXT,
            "max_text_chars": MAX_TEXT_CHARS,
            "voices": [],
            "note": "",
        }
        if config.vieneu.mode == "server":
            out["note"] = (
                "The voice server chooses the voices, so they cannot be listed here. "
                "Type a voice name to try it."
            )
            return out
        with self._lock:
            try:
                provider = self._get_provider()
                choose = getattr(provider, "list_voice_choices", None)
                pairs = choose() if choose else [(n, n) for n in provider.list_voices()]
                # the label is for showing; the id is what is sent back and spoken
                out["voices"] = [{"id": vid, "label": label} for label, vid in pairs]
            except TTSError as exc:
                out["note"] = str(exc)
        if not out["voices"] and not out["note"]:
            out["note"] = "No preset voices were reported. Type a voice name to try it."
        return out

    def _cache_path(self, voice: str, text: str, extension: str) -> Path:
        key = hashlib.sha1(f"{voice}\0{text}".encode()).hexdigest()[:20]
        return self.manager.work_root / CACHE_DIR / f"{key}{extension}"

    def sample(self, voice: str | None, text: str | None) -> Path:
        """The wav of ``text`` spoken by ``voice`` (made now, or taken from the cache)."""
        text = " ".join((text or "").split()) or DEFAULT_SAMPLE_TEXT
        if len(text) > MAX_TEXT_CHARS:
            raise BadRequest(f"Please use at most {MAX_TEXT_CHARS} characters for the sample.")
        voice = clean_voice(voice) or self.manager.config.vieneu.voice
        with self._lock:
            try:
                provider = self._get_provider()
                path = self._cache_path(voice, text, getattr(provider, "extension", ".wav"))
                if path.is_file() and path.stat().st_size > 0:
                    return path
                provider.synthesize(text, path, voice)
            except TTSVoiceError as exc:
                raise BadRequest(str(exc)) from exc
            except TTSSetupError as exc:
                raise UpstreamError(str(exc)) from exc
            except TTSError as exc:
                raise UpstreamError(f"The voice could not speak the sample: {exc}") from exc
        return path
