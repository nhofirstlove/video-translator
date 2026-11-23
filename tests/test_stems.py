"""Separated voice and music: the mix, the editor data and the volumes of each part."""

from __future__ import annotations

import pytest

from tests.e2e_support import tone_amplitude
from tests.test_editor_api import Env
from tests.test_mix import make_job, write_wav
from video_translator.config import AppConfig
from video_translator.stages.mix import MixStage, build_graph, find_beep_spans, separated_parts
from video_translator.stages.separate_audio import SeparateAudioStage


@pytest.fixture
def env(tmp_path, monkeypatch):
    environment = Env(tmp_path, monkeypatch)
    with environment.client:
        yield environment
        environment.manager.shutdown()


def add_stems(job, total=20.0):
    """Background = a 440 Hz tone, Chinese voice = an 880 Hz tone (the original holds both)."""
    job.separate_voice = True
    sep = job.work_dir / "separation"
    sep.mkdir(exist_ok=True)
    job.artifacts["background"] = write_wav(
        sep / "background.wav", total, freq=440, rate=44100, channels=2
    )
    job.artifacts["vocals"] = write_wav(sep / "vocals.wav", total, freq=880, rate=44100, channels=2)
    for kind in ("vocals", "background"):
        job.artifacts[f"{kind}_preview"] = SeparateAudioStage._preview(
            job, job.artifacts[kind], kind
        )
    return job


def test_graph_has_a_third_input_for_the_chinese_voice_only_when_asked():
    plain = build_graph([], AppConfig())
    assert "amix=inputs=2" in plain and "[2:a]" not in plain
    both = build_graph([], AppConfig(), {"original_voice_volume": 0.6}, vocals=True)
    assert "amix=inputs=3" in both and "[2:a]" in both and "volume=0.6000" in both


def test_mix_removes_the_chinese_voice_by_default_and_keeps_it_on_request(tmp_path):
    job = add_stems(make_job(tmp_path, total=8.0, speech=()), total=8.0)
    assert separated_parts(job) is not None
    MixStage().run(job, AppConfig())
    out = job.artifacts["mixed_audio"]
    music = tone_amplitude(out, 440, 2.0, 6.0)
    assert music > 1000 and tone_amplitude(out, 880, 2.0, 6.0) < music / 20
    assert "Chinese voice removed" in job.message
    job.audio_overrides = {"original_voice_volume": 1.0}
    MixStage().run(job, AppConfig())
    out = job.artifacts["mixed_audio"]
    assert tone_amplitude(out, 880, 2.0, 6.0) > 1000
    assert "Chinese voice kept" in job.message
    job.audio_overrides = {"background_volume": 0.0, "original_voice_volume": 1.0}
    MixStage().run(job, AppConfig())
    out = job.artifacts["mixed_audio"]
    assert tone_amplitude(out, 440, 2.0, 6.0) < tone_amplitude(out, 880, 2.0, 6.0) / 20


def test_without_separation_the_original_sound_is_used(tmp_path):
    job = make_job(tmp_path, total=8.0, speech=())
    assert separated_parts(job) is None
    MixStage().run(job, AppConfig())
    out = job.artifacts["mixed_audio"]
    assert tone_amplitude(out, 440, 2.0, 6.0) > 1000


def test_beeps_are_searched_in_the_original_chunks(tmp_path):
    from video_translator.models import Chunk

    job = make_job(tmp_path, total=4.0, speech=())
    (job.work_dir / "chunks").mkdir()
    (job.work_dir / "chunks_vocals").mkdir()
    original = write_wav(job.work_dir / "chunks" / "chunk_000.wav", 4.0, freq=1000, rate=16000)
    voice = write_wav(job.work_dir / "chunks_vocals" / "chunk_000.wav", 4.0, rate=16000)
    job.chunks = [Chunk(index=0, start=0.0, end=4.0, path=voice)]
    spans = find_beep_spans(job)
    assert original.is_file() and spans and spans[0][0] < 0.5 and spans[-1][1] > 3.5


# ----- the editor -------------------------------------------------------------------------


def make_stem_job(env: Env) -> str:
    job_id = env.make_job()
    job = env.job(job_id)
    add_stems(job, total=40.0)
    job.save()
    return job_id


def test_audio_settings_report_stems_and_keep_the_chinese_volume(env):
    job_id = env.make_job()
    url = f"/api/jobs/{job_id}/audio-settings"
    editor = env.client.get(f"/api/jobs/{job_id}/editor").json()
    assert editor["audio"]["has_stems"] is False and editor["audio"]["original_voice_volume"] == 0.0
    stem_id = make_stem_job(env)
    stem_url = f"/api/jobs/{stem_id}/audio-settings"
    editor = env.client.get(f"/api/jobs/{stem_id}/editor").json()
    assert editor["audio"]["has_stems"] is True
    body = {"voice_volume": 1.0, "background_volume": 0.8, "ducked_volume": 0.2}
    saved = env.client.put(stem_url, json={**body, "original_voice_volume": 0.4}).json()
    assert saved["original_voice_volume"] == 0.4 and saved["has_stems"] is True
    # a save without the new field keeps the Chinese volume
    again = env.client.put(stem_url, json={**body, "background_volume": 0.9}).json()
    assert again["original_voice_volume"] == 0.4 and again["background_volume"] == 0.9
    assert env.job(stem_id).audio_overrides["original_voice_volume"] == 0.4
    bad = env.client.put(stem_url, json={**body, "original_voice_volume": 5})
    assert bad.status_code == 422 or bad.status_code == 400
    plain = env.client.put(url, json=body).json()
    assert plain["has_stems"] is False


def test_waveforms_and_files_of_the_separated_parts(env):
    job_id = make_stem_job(env)
    waves = env.client.get(f"/api/jobs/{job_id}/waveforms").json()
    assert waves["stems"] is True and waves["voice_zh"]["peaks"] and waves["background"]["peaks"]
    for kind in ("vocals", "background"):
        res = env.client.get(f"/api/jobs/{job_id}/media/stem/{kind}")
        assert res.status_code == 200 and len(res.content) > 1000
    assert env.client.get(f"/api/jobs/{job_id}/media/stem/other").status_code == 404
    plain_id = env.make_job()
    assert env.client.get(f"/api/jobs/{plain_id}/media/stem/vocals").status_code == 404
    plain = env.client.get(f"/api/jobs/{plain_id}/waveforms").json()
    assert plain["stems"] is False and "voice_zh" not in plain


def test_default_config_keeps_the_chinese_voice_out():
    assert AppConfig().audio.original_voice_volume == 0.0
