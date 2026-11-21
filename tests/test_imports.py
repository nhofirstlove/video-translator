from __future__ import annotations

import importlib

from video_translator.stages import ALL_STAGES

STAGE_MODULES = [
    "extract_audio",
    "separate_audio",
    "transcribe",
    "translate",
    "synthesize",
    "align",
    "mix",
    "subtitles",
    "render",
]


def test_every_stage_module_imports():
    for module in STAGE_MODULES:
        importlib.import_module(f"video_translator.stages.{module}")


def test_stages_listed_in_pipeline_order():
    assert [stage.name for stage in ALL_STAGES] == STAGE_MODULES


def test_web_app_has_health_route():
    from video_translator.web.app import create_app

    paths = {route.path for route in create_app().routes}
    assert {"/health", "/jobs", "/jobs/{job_id}"} <= paths


def test_resolve_device_cpu():
    from video_translator.utils.device import resolve_device

    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in {"cuda", "cpu"}
