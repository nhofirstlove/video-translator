# Architecture

## Pipeline (version 1)

```
 input.mp4
    |
    v
[extract_audio] --> audio/audio.wav (44.1 kHz stereo) + chunks/chunk_NNN.wav (16 kHz mono,
    |                                                    overlapping by 5 s)
    v
[transcribe]    per chunk: faster-whisper (local) times each line, Gemini corrects the words --> zh segments (video-absolute times)
    |
    v
[translate]     Gemini, batches of 40 with 3 lines of context --> vi text
    |
    v
[review]        writes review/review.txt and STOPS (AWAITING_REVIEW) until approved
    |
    v
[subtitles]     --> subtitles/<name>.vi.srt
    |
    v
[synthesize]    VieNeu-TTS, one clip per segment (resumable) --> tts/
    |
    v
[align]         fit clips to their slots --> aligned/ + voice_track.wav (full length)
    |
    v
[mix]           original audio ducked under the voice --> mixed.wav
    |
    v
[render]        input video (copied) + mixed.wav --> <output>/<name>.vi.mp4 (+ .vi.srt)
```

`separate_audio` (vocals vs. background, version 2) is defined but not part of the version 1
list (`stages.V1_STAGES`, used by `build_stages`). Authoritative artifact names and pause
rules: [contracts.md](contracts.md).

## Data flow

All stages share one `Job` object (`models.py`). Each stage receives the job and the
`AppConfig`, does its work, and returns the job:

- `job.chunks` are audio ranges made by `extract_audio`; `job.segments` holds timed
  `Segment`s: `transcribe` creates them (`text_zh`), `translate` fills `text_vi`,
  `synthesize` sets `audio_path`, `align` sets `aligned_path`.
- `job.artifacts` maps names (`audio`, `voice_track`, `mixed_audio`, `srt`, `output`,
  `srt_output`) to files.
- `Pipeline.run(job, on_progress)` runs the stages in order, skips names already in
  `job.completed_stages`, saves `job.json` after each stage and reports progress as
  `(stage_name, completed, total)`. `resume` is the same call.

### Pausing and the review gate

A stage stops the pipeline cleanly by raising `PauseRequested(status, message)`. The job gets
that status and message, is saved, and the stage is not marked complete, so it runs again on
resume. Two uses:

- `review` raises `AWAITING_REVIEW` until `job.review_approved` is set (`resume --approve` or
  the web Approve button). On approval it reads `review/review.txt` if the user changed it
  after it was written (web edits go straight into `job.segments`).
- `PAUSED`: Gemini daily quota (transcribe/translate, after saving finished chunks/batches).

Any other exception makes the job `FAILED`; it can be resumed after the problem is fixed. A
voice (TTS) error fails the synthesize stage with a message naming the failing line; clips
already made are kept.

AI steps (ASR, translation, TTS) never hard-code a model. A stage asks the provider registry
for the class named in `config.providers.<kind>`, so implementations are interchangeable.
Importing `video_translator.providers` (or any stage) registers the built-in ones.

## The interactive editor

`/editor/<job id>` is a second page of the web UI for fixing a job after (or before) dubbing.
Contract between the pieces: `docs/editor-api.md`.

- `web/editor_api.py`: the JSON API (lines, per-line voice and speed, volumes, apply, media with
  Range support). Edits go through `JobManager.editing()` (per-job lock, 409 while running).
- `synth_line.py`: speaks ONE line (one TTS call, clip re-fitted by `stages.align.fit_segment`).
  Free and local, so the editor needs no confirmation.
- `web/jobs.py`: `apply_edits()` starts the "apply" pipeline: subtitles, align, mix, render
  (never synthesize), using the job's `audio_overrides` volumes and per-line `speed`.
- `web/templates/editor.html`, `static/editor.js` (page, player, lines), `static/editor_audio.js`
  (listen, regenerate, speed, volumes, Web Audio dub preview, Apply flow).
- `tests/test_editor_browser.py` drives all of it in a real Chromium (Playwright) with the real
  stages and fake providers; it is skipped when Playwright or Chromium is missing.

## Adding a stage

1. Create `stages/<name>.py` with a `Stage` subclass (`name`, `run(job, config) -> Job`).
2. Add the class to `ALL_STAGES` in `stages/__init__.py` at the right position.
3. Update `tests/test_imports.py` (expected order).

## Adding a provider

1. Pick the area (`separation`, `asr`, `translation`, `tts`) and subclass its `base.py` interface.
2. Decorate the class: `@register("<area>", "<name>")`.
3. Import the module in the area's `__init__.py` so it registers on import.
4. Allow its name in `config.py` (`ProvidersConfig`) and select it in `config/default.yaml`
   under `providers.<area>`.
5. Add third-party dependencies to `pyproject.toml`.

## Work-dir layout

Each job gets its own folder under `paths.work` (default `data/work/`):

```
data/work/<job-id>/
    job.json             full job state (written after every stage)
    audio/audio.wav      extract_audio
    chunks/              extract_audio (chunk_NNN.wav, plus chunk_NNN.whisper.json: Whisper timing cache)
    transcripts/         transcribe (one json per chunk, reused on resume)
    translations/        translate (one json per batch, reused on resume)
    review/review.txt    review (the file the user edits)
    subtitles/           subtitles (<name>.vi.srt)
    tts/                 synthesize (one wav per segment)
    aligned/             align (one wav per segment)
    voice_track.wav      align
    mixed.wav            mix
```

The final files are written to `paths.output` (default `data/output/`) as `<name>.vi.mp4`
and `<name>.vi.srt`.

## Text to speech (VieNeu-TTS)

`providers/tts/vieneu.py` (`providers.tts: vieneu`) is free, local and open source. Config
section `vieneu`: `mode` (`library`: the `vieneu` Python package, one shared model per process
created on first use under a lock, calls serialized; or `server`: HTTP POST to
`<server_url>/v1/audio/speech`, standard library only), `voice` (preset name), `precision`,
`server_url`, `timeout_seconds`. Clips are 48 kHz mono `.wav` files. The package is imported
lazily; errors derive from `TTSError` (`TTSSetupError` for a missing package, model download
or unreachable server, `TTSVoiceError` for an unknown voice). Tests inject a fake transport
(`synthesize(text, voice, out_path)` and `list_voices()`).

## Tests

`tests/test_e2e.py` and `tests/test_e2e_web_cli.py` run the real pipeline (real ffmpeg, real
stages) on a synthetic video with fake Gemini and VieNeu transports from
`tests/e2e_support.py`; no network is used.
