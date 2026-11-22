from __future__ import annotations

import importlib

from video_translator.config import AppConfig
from video_translator.stages import ALL_STAGES, V1_STAGES, build_stages

STAGE_MODULES = [
    "extract_audio",
    "separate_audio",
    "transcribe",
    "translate",
    "review",
    "subtitles",
    "synthesize",
    "align",
    "mix",
    "render",
]


def test_every_stage_module_imports():
    for module in STAGE_MODULES:
        importlib.import_module(f"video_translator.stages.{module}")


def test_stages_listed_in_pipeline_order():
    assert [stage.name for stage in ALL_STAGES] == STAGE_MODULES


def test_v1_stages_order_and_build():
    names = [stage.name for stage in V1_STAGES]
    assert names == [
        "extract_audio",
        "transcribe",
        "translate",
        "review",
        "subtitles",
        "synthesize",
        "align",
        "mix",
        "render",
    ]
    assert "separate_audio" not in names
    assert [type(s) for s in build_stages(AppConfig())] == V1_STAGES


def test_web_app_has_real_routes():
    from video_translator.web.app import create_app

    paths = {route.path for route in create_app().routes}
    assert {
        "/health",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/segments",
        "/api/jobs/{job_id}/approve",
        "/api/jobs/{job_id}/download/{kind}",
        "/api/doctor",
        "/api/config/summary",
    } <= paths


def test_resolve_device_cpu():
    from video_translator.utils.device import resolve_device

    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in {"cuda", "cpu"}
