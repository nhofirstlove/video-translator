"""Synthesize ONE line with the configured TTS provider (used by the web editor).

The raw clip goes to ``<work_dir>/tts/seg_XXXXX<ext>``; the line is then re-fitted to its time
slot (aligned clip). The caller saves the job.
"""

from __future__ import annotations

from collections.abc import Callable

from video_translator.config import AppConfig
from video_translator.models import Job, Segment
from video_translator.providers import registry
from video_translator.providers.tts import vieneu as _vieneu  # noqa: F401  (registers)
from video_translator.providers.tts.base import TTSProvider
from video_translator.stages.align import fit_segment

TTSFactory = Callable[[AppConfig], TTSProvider]


class LineSynthError(RuntimeError):
    """The line cannot be synthesized."""


def default_tts_factory(config: AppConfig) -> TTSProvider:
    return registry.get("tts", config.providers.tts)(config=config)


def line_chars(seg: Segment) -> int:
    """Number of characters of the Vietnamese text of this line."""
    return len(seg.text_vi.strip())


def synthesize_line(
    job: Job,
    index: int,
    config: AppConfig,
    tts_factory: TTSFactory | None = None,
) -> Segment:
    """Speak segment ``index`` again and re-fit its clip. Errors from the provider propagate
    (``TTSError`` and its subclasses)."""
    seg = job.segments[index]
    text = seg.text_vi.strip()
    if not text:
        raise LineSynthError("This line has no Vietnamese text to speak.")
    provider = (tts_factory or default_tts_factory)(config)
    ext = getattr(provider, "extension", ".wav")
    tts_dir = job.work_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    out = tts_dir / f"seg_{index:05d}{ext}"
    provider.synthesize(text, out, job.voice)  # None = the configured voice
    seg.audio_path = out
    seg.audio_stale = False
    fit_segment(job, index)
    return seg
