"""Settings model mirroring ``config/default.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path("config/default.yaml")


class ProviderConfig(BaseModel):
    """Which provider implementation to use, plus its free-form options."""

    name: str
    options: dict = Field(default_factory=dict)


class ProvidersConfig(BaseModel):
    separation: ProviderConfig = ProviderConfig(name="demucs")
    asr: ProviderConfig = ProviderConfig(name="faster_whisper")
    translation: ProviderConfig = ProviderConfig(name="llm")
    tts: ProviderConfig = ProviderConfig(name="placeholder")


class SubtitlesConfig(BaseModel):
    srt: bool = True
    burn_in: bool = False


class AudioConfig(BaseModel):
    background_volume: float = 1.0
    voice_volume: float = 1.0


class PathsConfig(BaseModel):
    input_dir: Path = Path("data/input")
    work_dir: Path = Path("data/work")
    output_dir: Path = Path("data/output")


class AppConfig(BaseModel):
    device: Literal["auto", "cuda", "cpu"] = "auto"
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load a YAML config file; fall back to built-in defaults if none exists.

    Placeholder: no environment-variable overrides or ``.env`` loading yet.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return AppConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
