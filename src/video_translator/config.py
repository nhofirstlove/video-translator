"""Settings model mirroring ``config/default.yaml``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_translator.paths import project_root

CONFIG_ENV_VAR = "VT_CONFIG"
LOCAL_CONFIG_NAME = "local.yaml"


class ConfigError(ValueError):
    """The configuration could not be loaded or is invalid."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvidersConfig(_Model):
    # whisper = Whisper alone (local, free); whisper_gemini = Whisper times, Gemini writes the words
    asr: Literal["whisper", "whisper_gemini", "gemini"] = "whisper"
    # ninerouter = the free models in 9Router first, Gemini only when all of them failed
    translation: Literal["ninerouter", "gemini"] = "ninerouter"
    tts: Literal["vieneu"] = "vieneu"


class GeminiConfig(_Model):
    model: str = "gemini-3.6-flash"
    requests_per_minute: int = Field(10, gt=0)


class NineRouterConfig(_Model):
    """9Router: a program on this computer that fronts many AI services (free ones included)."""

    base_url: str = "http://localhost:20128/v1"
    api_key_env: str = "NINEROUTER_API_KEY"  # optional: 9Router accepts any key unless it asks
    models: list[str] = Field(default_factory=lambda: ["free-combo"])  # tried in this order
    requests_per_minute: int = Field(20, gt=0)
    timeout_seconds: float = Field(120.0, gt=0)
    batch_size: int = Field(20, gt=0, le=100)  # lines per request; free models like small ones


class WhisperConfig(_Model):
    """Local speech timing (faster-whisper, CPU). Used by providers.asr = whisper_gemini."""

    model: str = "medium"  # tiny, base, small, medium (about 0.8 GB), large-v3 (slow on CPU)
    compute_type: Literal["int8", "float32"] = "int8"
    beam_size: int = Field(1, gt=0)
    cpu_threads: int = Field(0, ge=0)  # 0 = let the library choose
    max_segment_seconds: float = Field(12.0, gt=1.5)
    detect_beeps: bool = True  # cut lines at bleep tones so the bleeped word gets its own line
    pause_seconds: float = Field(0.4, ge=0.1, le=3.0)  # a pause this long starts a new line
    vad_threshold: float = Field(0.3, gt=0.0, lt=1.0)  # lower = hears quieter speech
    model_dir: str | None = None  # where to keep the downloaded model (None = default cache)


class VieNeuConfig(_Model):
    mode: Literal["library", "server"] = "library"
    voice: str = "Trúc Ly"
    precision: Literal["fp32", "int8"] = "fp32"
    server_url: str = "http://localhost:8000"
    timeout_seconds: float = Field(300, gt=0)


class BlurConfig(_Model):
    """A blurred box over the burned-in Chinese subtitles, with the Vietnamese ones on top.

    Sizes and positions are in pixels of the ORIGINAL video. ``x`` / ``y`` left empty mean
    "centred" and "near the bottom". Switching it on makes the render re-encode the video.
    """

    enabled: bool = False
    x: int | None = Field(None, ge=0)
    y: int | None = Field(None, ge=0)
    width: int = Field(1100, ge=16)
    height: int = Field(130, ge=16)
    strength: int = Field(70, ge=1, le=100)  # 100 = strongest blur
    tint: float = Field(0.0, ge=0.0, le=0.9)  # optional dark layer over the blur, 0 = none
    font_size: int = Field(40, ge=8, le=300)  # Vietnamese text height in pixels of the video
    font: str = "Arial"


class SubtitlesConfig(_Model):
    srt: bool = True
    burn_in: bool = False


class AudioConfig(_Model):
    voice_volume: float = Field(1.0, ge=0)
    background_volume: float = Field(1.0, ge=0)
    ducked_volume: float = Field(0.25, ge=0)
    remove_beeps: bool = (
        True  # silence the original bleep tones (the Vietnamese voice says the word)
    )


class PathsConfig(_Model):
    input: Path = Path("data/input")
    work: Path = Path("data/work")
    output: Path = Path("data/output")


class AppConfig(_Model):
    device: Literal["auto", "cuda", "cpu"] = "auto"
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    ninerouter: NineRouterConfig = Field(default_factory=NineRouterConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    vieneu: VieNeuConfig = Field(default_factory=VieNeuConfig)
    chunk_minutes: int = Field(5, gt=0)
    review: bool = True
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)
    blur: BlurConfig = Field(default_factory=BlurConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` merged in; nested dicts are merged key by key."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def find_default_config() -> Path | None:
    """Locate the default config: ``VT_CONFIG``, else config/default.yaml of the project."""
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    candidate = project_root() / "config" / "default.yaml"
    return candidate if candidate.is_file() else None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at the top level.")
    return data


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load the settings.

    Order: built-in defaults, then the default YAML (``path``, else ``VT_CONFIG``, else the
    project's ``config/default.yaml``), then ``local.yaml`` next to it if present.
    """
    base_path = Path(path) if path else find_default_config()
    data: dict[str, Any] = {}
    if base_path is not None:
        if not base_path.is_file():
            raise ConfigError(f"Config file not found: {base_path}")
        data = _read_yaml(base_path)
        local_path = base_path.parent / LOCAL_CONFIG_NAME
        if local_path.is_file() and local_path.resolve() != base_path.resolve():
            data = deep_merge(data, _read_yaml(local_path))
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration: {problems}") from exc


def save_local_override(section: str, values: dict[str, Any]) -> Path:
    """Merge ``{section: values}`` into ``config/local.yaml`` (next to the default config).

    Used by the editor's "save as default" button. Comments in local.yaml are not kept.
    """
    base = find_default_config()
    if base is None:
        raise ConfigError("There is no config folder to save the default into.")
    target = base.parent / LOCAL_CONFIG_NAME
    data = _read_yaml(target) if target.is_file() else {}
    data = deep_merge(data, {section: values})
    try:
        AppConfig.model_validate(deep_merge(_read_yaml(base), data))
    except ValidationError as exc:
        raise ConfigError(f"Invalid value: {exc.errors()[0]['msg']}") from exc
    tmp = target.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise ConfigError(f"Cannot write {target}: {exc}") from exc
    return target
