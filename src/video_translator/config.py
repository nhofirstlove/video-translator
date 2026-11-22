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
    asr: Literal["gemini"] = "gemini"
    translation: Literal["gemini"] = "gemini"
    tts: Literal["elevenlabs"] = "elevenlabs"


class GeminiConfig(_Model):
    model: str = "gemini-2.5-flash"
    requests_per_minute: int = Field(10, gt=0)


class ElevenLabsConfig(_Model):
    voice: str = ""
    model: str = "eleven_multilingual_v2"
    monthly_character_budget: int = Field(10000, ge=0)


class SubtitlesConfig(_Model):
    srt: bool = True
    burn_in: bool = False


class AudioConfig(_Model):
    voice_volume: float = Field(1.0, ge=0)
    background_volume: float = Field(1.0, ge=0)
    ducked_volume: float = Field(0.25, ge=0)


class PathsConfig(_Model):
    input: Path = Path("data/input")
    work: Path = Path("data/work")
    output: Path = Path("data/output")


class AppConfig(_Model):
    device: Literal["auto", "cuda", "cpu"] = "auto"
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)
    chunk_minutes: int = Field(10, gt=0)
    review: bool = True
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)
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
