"""Voice separation with Demucs (runs on this computer; CPU is fine but slow).

The audio is cut into slices of two minutes, each slice is split into "voice" and "everything
else", and the pieces are joined again. Every finished slice is kept on disk, so a stopped job
continues where it stopped instead of starting over. Only the 44.1 kHz stereo 16-bit wav that
``extract_audio`` writes is read and written (no torchaudio file saving is used).
"""

from __future__ import annotations

import os
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from video_translator.providers.registry import register
from video_translator.providers.separation.base import SeparationProvider

SLICE_SECONDS = 120.0
INSTALL_HINT = (
    "Voice separation needs the Demucs package. In the activated virtual environment run "
    "`pip install demucs` (it also installs PyTorch, a download of a few hundred MB), then try "
    "again."
)

ProgressCallback = Callable[[int, int], None]


class SeparationError(RuntimeError):
    """Separation could not run."""


class Backend(Protocol):
    """Splits one slice. ``wav`` is float32 ``(channels, samples)`` at ``samplerate``."""

    samplerate: int

    def split(self, wav: Any) -> tuple[Any, Any]:
        """``(vocals, background)``, both float32 with the same shape as ``wav``."""


def load_backend(model_name: str, device: str) -> Backend:
    """The real Demucs model (downloaded on first use)."""
    try:
        import torch
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
    except ImportError as exc:
        raise SeparationError(INSTALL_HINT) from exc
    try:
        model = get_model(model_name)
        model.eval()
    except Exception as exc:  # download or load problems
        raise SeparationError(
            f"Could not load the Demucs model {model_name!r}: {exc}. The first run downloads it "
            "(about 80 MB), so connect to the internet once and try again."
        ) from exc
    vocals_index = model.sources.index("vocals")

    class _Demucs:
        samplerate = int(model.samplerate)

        def split(self, wav: Any) -> tuple[Any, Any]:
            mix = torch.from_numpy(wav)
            ref = mix.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                out = apply_model(
                    model, ((mix - mean) / std)[None], device=device, split=True, overlap=0.25
                )[0]
            out = out * std + mean
            vocals = out[vocals_index]
            return vocals.numpy(), (out.sum(0) - vocals).numpy()

    return _Demucs()


def _to_int16(samples: Any) -> bytes:
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped.T * 32767.0).astype("<i2").tobytes()  # interleaved frames


def _read_slice(path: Path, start: float, seconds: float) -> tuple[Any, int]:
    """Float32 ``(2, samples)`` and the sample rate, from a 16-bit stereo wav."""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise SeparationError(f"{path.name} is not a 16-bit wav file")
        w.setpos(min(int(start * rate), w.getnframes()))
        raw = w.readframes(int(seconds * rate))
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    data = data[: len(data) - len(data) % channels].reshape(-1, channels).T
    if channels == 1:
        data = np.repeat(data, 2, axis=0)
    return np.ascontiguousarray(data[:2]), rate


def _write_wav(path: Path, rate: int, frames: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    os.replace(tmp, path)


def _join(parts: list[Path], target: Path, rate: int) -> None:
    tmp = target.with_name(target.name + ".part")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        for part in parts:
            with wave.open(str(part), "rb") as piece:
                out.writeframes(piece.readframes(piece.getnframes()))
    os.replace(tmp, target)


@register("separation", "demucs")
class DemucsSeparation(SeparationProvider):
    """Demucs two-stem mode: the voice, and everything else."""

    def __init__(
        self,
        device: str = "cpu",
        model: str = "htdemucs",
        backend: Backend | None = None,
        progress: ProgressCallback | None = None,
        slice_seconds: float = SLICE_SECONDS,
        **options: Any,
    ) -> None:
        super().__init__(device=device, **options)
        self.model = model
        self._backend = backend
        self.progress = progress
        self.slice_seconds = slice_seconds

    def separate(self, audio_path: Path, out_dir: Path) -> tuple[Path, Path]:
        audio_path, out_dir = Path(audio_path), Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        vocals, background = out_dir / "vocals.wav", out_dir / "background.wav"
        if vocals.is_file() and background.is_file():
            return vocals, background
        with wave.open(str(audio_path), "rb") as w:
            rate, total = w.getframerate(), w.getnframes() / w.getframerate()
        count = max(1, int(-(-total // self.slice_seconds)))
        backend = self._backend or load_backend(self.model, self.device)
        if backend.samplerate != rate:
            raise SeparationError(
                f"The Demucs model works at {backend.samplerate} Hz but the audio is {rate} Hz"
            )
        voice_parts, other_parts = [], []
        for i in range(count):
            v_part, o_part = out_dir / f"vocals_{i:04d}.wav", out_dir / f"other_{i:04d}.wav"
            if not (v_part.is_file() and o_part.is_file()):
                wav, _ = _read_slice(audio_path, i * self.slice_seconds, self.slice_seconds)
                v, o = backend.split(wav)
                _write_wav(v_part, rate, _to_int16(v))
                _write_wav(o_part, rate, _to_int16(o))
            voice_parts.append(v_part)
            other_parts.append(o_part)
            if self.progress:
                self.progress(i + 1, count)
        _join(voice_parts, vocals, rate)
        _join(other_parts, background, rate)
        for part in [*voice_parts, *other_parts]:
            part.unlink(missing_ok=True)
        return vocals, background
