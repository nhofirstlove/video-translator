"""Shared fakes for the end-to-end tests: a synthetic video plus fake Gemini and VieNeu.

Nothing here touches the network. The fakes replace only the lowest layer (the transports the
real ``GeminiClient`` and ``VieNeuTTS`` call), so the real stages, providers, prompts,
parsers, retry/quota handling and the pipeline all run.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from video_translator.config import AppConfig
from video_translator.providers import registry
from video_translator.providers.gemini_client import QuotaExhaustedError
from video_translator.providers.translation.gemini import GeminiTranslation
from video_translator.utils.ffmpeg import find_ffprobe, run_ffmpeg

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

VIDEO_SECONDS = 40
BED_FREQS = (220, 330)  # the "music" under the speech
VOICE_FREQ = 1000  # the fake "VieNeu voice"
VOICE_NAME = "Mai Anh"
SECONDS_PER_CHAR = 0.08
CHUNK_MINUTES = 0.2  # 12 s chunks: a 40 s video gives 3 overlapping chunks
CHUNK_SECONDS = CHUNK_MINUTES * 60
TRANSLATE_BATCH = 3

# Ground truth speech: utterance k is spoken at [4k + 0.5, 4k + 3.0] seconds, k = 0..8.
UTTERANCES = [(4 * k + 0.5, 4 * k + 3.0, f"第{k}句话") for k in range(9)]


def vietnamese_for(zh: str) -> str:
    number = re.search(r"\d+", zh).group()
    return f"Đây là câu thứ {number} của đoạn thử nghiệm"


def make_video(path: Path, seconds: int = VIDEO_SECONDS) -> Path:
    """Synthetic h264 + aac video: colour test pattern and a two-tone music bed."""
    tones = "+".join(f"sin(2*PI*{f}*t)*0.2" for f in BED_FREQS)
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"aevalsrc='{tones}':s=44100:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        desc="make test video",
    )  # fmt: skip
    return path


def probe_streams(path: Path) -> dict[str, dict]:
    """``{codec_type: {codec_name, duration}}`` from ffprobe."""
    out = subprocess.run(
        [
            find_ffprobe(), "-v", "error", "-print_format", "json",
            "-show_entries", "stream=codec_type,codec_name,duration", str(path),
        ],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return {
        s["codec_type"]: {"codec_name": s["codec_name"], "duration": float(s["duration"])}
        for s in json.loads(out)["streams"]
    }


def tone_amplitude(wav_path: Path, freq: float, start: float, end: float) -> float:
    """Amplitude of a sine of ``freq`` Hz in ``wav_path`` between two times (Goertzel)."""
    with wave.open(str(wav_path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        w.setpos(int(start * rate))
        raw = w.readframes(int((end - start) * rate))
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)[::channels]
    n = len(samples)
    coeff = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + coeff * s1 - s2, s1
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return 2 * math.sqrt(max(power, 0.0)) / n


class FakeGemini:
    """Stands in for the Gemini transport: answers transcription and translation requests."""

    def __init__(self) -> None:
        self.asr_calls: list[Path] = []
        self.translate_prompts: list[str] = []
        self.translate_quota_after: int | None = None  # raise QuotaExhaustedError after N calls

    @property
    def total_calls(self) -> int:
        return len(self.asr_calls) + len(self.translate_prompts)

    def generate_text(self, prompt: str, *, audio_path=None, json_mode: bool = False) -> str:
        if audio_path is not None:
            return self._transcribe(Path(audio_path))
        return self._translate(prompt)

    def _transcribe(self, audio_path: Path) -> str:
        self.asr_calls.append(audio_path)
        index = int(re.search(r"chunk_(\d+)", audio_path.name).group(1))
        start = index * CHUNK_SECONDS
        with wave.open(str(audio_path), "rb") as w:
            length = w.getnframes() / w.getframerate()
        segments = [
            {"start": round(a - start, 2), "end": round(b - start, 2), "text": zh}
            for a, b, zh in UTTERANCES
            if a >= start and b <= start + length  # only whole sentences inside this chunk
        ]
        return json.dumps({"segments": segments}, ensure_ascii=False)

    def _translate(self, prompt: str) -> str:
        if (
            self.translate_quota_after is not None
            and len(self.translate_prompts) >= self.translate_quota_after
        ):
            raise QuotaExhaustedError("Gemini daily quota is used up (fake).")
        self.translate_prompts.append(prompt)
        match = re.search(r"Input JSON:\n(.*)\n\nReply with JSON only", prompt, re.DOTALL)
        payload = json.loads(match.group(1))
        items = [{"id": s["id"], "vi": vietnamese_for(s["zh"])} for s in payload["segments"]]
        return json.dumps({"segments": items}, ensure_ascii=False)


class FakeVieNeu:
    """Stands in for the VieNeu transport: a real wav whose length follows the text length."""

    def __init__(self, work_dir: Path = Path(".")) -> None:
        self.work_dir = work_dir
        self.calls: list[tuple[str, str]] = []  # (text, voice)
        self.fail_on: str | None = None  # raise TTSError when the text contains this

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        from video_translator.providers.tts.base import TTSError

        self.calls.append((text, voice))
        if self.fail_on is not None and self.fail_on in text:
            raise TTSError("the fake voice engine broke")
        seconds = max(0.3, len(text) * SECONDS_PER_CHAR)
        rate = 48000
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * VOICE_FREQ * n / rate)))
            for n in range(int(seconds * rate))
        )
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)

    def list_voices(self) -> list[tuple[str, str]]:
        return [("Minh Quân", "minh-quan"), ("Mai Anh", "mai-anh"), ("Trúc Ly", "truc-ly")]


class Fakes:
    def __init__(self, gemini: FakeGemini, tts: FakeVieNeu) -> None:
        self.gemini = gemini
        self.tts = tts


def make_config(tmp_path: Path, **overrides) -> AppConfig:
    """Config for tests: private folders, tiny chunks, fast rate limit, a voice name."""
    config = AppConfig()
    config.providers.asr = "gemini"  # the old Gemini-only path: no Whisper model in these tests
    config.providers.translation = "gemini"  # the tests fake Gemini, not 9Router
    config.paths.input = tmp_path / "data" / "input"
    config.paths.work = tmp_path / "data" / "work"
    config.paths.output = tmp_path / "data" / "output"
    config.gemini.requests_per_minute = 1000
    config.vieneu.voice = VOICE_NAME
    config = config.model_copy(update={"chunk_minutes": CHUNK_MINUTES})  # no validation: < 1 min
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def install_fakes(monkeypatch, tmp_path: Path) -> Fakes:
    """Replace the Gemini and VieNeu transports (and use small translation batches)."""
    from video_translator.providers import gemini_client
    from video_translator.providers.tts import vieneu

    fakes = Fakes(FakeGemini(), FakeVieNeu(tmp_path / "fake_tts"))
    monkeypatch.setattr(gemini_client, "_SdkTransport", lambda api_key, model: fakes.gemini)
    monkeypatch.setattr(vieneu, "_LibraryTransport", lambda precision="fp32": fakes.tts)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class SmallBatchTranslation(GeminiTranslation):
        def __init__(self, **kwargs):
            super().__init__(batch_size=TRANSLATE_BATCH, **kwargs)

    registry.get("translation", "gemini")  # make sure the real one is registered first
    monkeypatch.setitem(registry._REGISTRY["translation"], "gemini", SmallBatchTranslation)
    return fakes
