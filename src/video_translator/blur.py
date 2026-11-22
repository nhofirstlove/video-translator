"""The blur box over the Chinese subtitles, and the Vietnamese subtitles drawn on top of it.

Pure helpers (no ffmpeg run here): settings -> box in pixels, an ASS subtitle file and the ffmpeg
filter graph. ``stages/render.py`` runs them. All sizes are pixels of the original video.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from video_translator.config import AppConfig, BlurConfig, ConfigError
from video_translator.models import Segment

BOTTOM_MARGIN_RATIO = 0.06  # default box sits this far above the bottom edge
SIGMA_PER_STRENGTH = 0.4  # strength 100 = gaussian sigma 40 px: unreadable text


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int


def effective_blur(config: AppConfig, overrides: dict[str, Any] | None = None) -> BlurConfig:
    """The global ``config.blur`` with this video's saved choices on top."""
    values = {**config.blur.model_dump(), **(overrides or {})}
    try:
        return BlurConfig.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(f"Invalid blur settings: {exc.errors()[0]['msg']}") from exc


def _even(value: int) -> int:
    return value - (value % 2)


def resolve_box(blur: BlurConfig, frame_width: int, frame_height: int) -> Box:
    """The box in pixels, kept inside the picture (defaults: centred, near the bottom)."""
    width = max(2, _even(min(blur.width, frame_width)))
    height = max(2, _even(min(blur.height, frame_height)))
    x = (frame_width - width) // 2 if blur.x is None else blur.x
    if blur.y is None:
        y = frame_height - height - round(frame_height * BOTTOM_MARGIN_RATIO)
    else:
        y = blur.y
    x = _even(max(0, min(x, frame_width - width)))
    y = _even(max(0, min(y, frame_height - height)))
    return Box(x, y, width, height)


def sigma(strength: int) -> float:
    return round(max(1, strength) * SIGMA_PER_STRENGTH, 2)


def _ass_time(seconds: float) -> str:
    centis = max(0, int(round(seconds * 100)))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return "\\N".join(part.strip() for part in text.splitlines() if part.strip())


def build_ass(
    segments: list[Segment], blur: BlurConfig, box: Box, frame_width: int, frame_height: int
) -> str:
    """An ASS subtitle file: Vietnamese text, white with a dark outline, centred in the box."""
    outline = max(1, round(blur.font_size / 14))
    cx, cy = box.x + box.width // 2, box.y + box.height // 2
    margin_l, margin_r = box.x, max(0, frame_width - box.x - box.width)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {frame_width}\n"
        f"PlayResY: {frame_height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Vi,{blur.font},{blur.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,5,{margin_l},{margin_r},0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for seg in segments:
        text = _ass_text(seg.text_vi)
        if not text or seg.end <= seg.start:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(seg.start)},{_ass_time(seg.end)},Vi,,0,0,0,,"
            f"{{\\an5\\pos({cx},{cy})}}{text}\n"
        )
    return header + "".join(lines)


def escape_filter_path(path: str | Path) -> str:
    """A file path as a quoted ffmpeg filter option value (works for Windows ``C:\\...``)."""
    text = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{text}'"


def build_filter_graph(blur: BlurConfig, box: Box, ass_path: str | Path) -> str:
    """ffmpeg ``-filter_complex``: blur the box, optional dark tint, then draw the subtitles.

    Input ``[0:v]``, output ``[v]``.
    """
    steps = [
        "[0:v]split=2[base][tmp]",
        f"[tmp]crop={box.width}:{box.height}:{box.x}:{box.y},"
        f"gblur=sigma={sigma(blur.strength)}:steps=2[blurred]",
    ]
    last = "blurred"
    if blur.tint > 0:
        steps.append(
            f"[{last}]drawbox=x=0:y=0:w=iw:h=ih:color=black@{blur.tint:.2f}:t=fill[tinted]"
        )
        last = "tinted"
    steps.append(f"[base][{last}]overlay={box.x}:{box.y}[boxed]")
    steps.append(f"[boxed]ass={escape_filter_path(ass_path)}[v]")
    return ";".join(steps)
