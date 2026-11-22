# Shared contracts (version 1)

## Artifact keys (`Job.artifacts`)
- `audio`: full-length 44.1 kHz stereo wav extracted from the video
- `voice_track`: aligned Vietnamese voice-only wav, full length
- `mixed_audio`: final audio
- `srt`: Vietnamese subtitles file
- `output`: final mp4 (`<output dir>/<name>.vi.mp4`)
- `srt_output`: copy of the subtitles next to the final mp4 (set by `render`)

## Job fields (`models.py`)
`id, input_path, work_dir, status, segments, artifacts, chunks, completed_stages,
review_approved, error, message, current_stage`.
`Chunk(index, start, end, path)`: audio of a time range (seconds in the full video).
`Segment`: `start, end, text_zh, text_vi, audio_path, aligned_path, flags` (e.g. `"too_long"`).
`JobStatus`: pending, running, awaiting_review, paused, done, failed.
`Job.create(input, work_root)`, `job.save()` / `Job.load(work_dir)` write `work_dir/job.json`.

## Stage order (`stages.V1_STAGES`, `build_stages(config)`)
extract_audio, transcribe, translate, review, subtitles, synthesize, align, mix, render.
`SeparateAudioStage` is version 2 and not in V1.

## Pipeline
`Pipeline(stages, config).run(job, on_progress)` / `.resume(job)`: saves after each stage,
skips names in `job.completed_stages`, sets `job.current_stage`. A stage that raises
anything but `PauseRequested` makes the job FAILED (`job.error`, re-raised). Stages must be
resumable: do not redo finished work (check files/segments already filled in).

## Pausing
`raise PauseRequested(JobStatus.AWAITING_REVIEW | JobStatus.PAUSED, "message for the user")`
(from `stages.base`). The pipeline stores status and message, saves, and returns the job.
The stage is not marked complete, so it runs again on `resume`. Convert
`QuotaExhaustedError` (Gemini daily quota) into `PAUSED`; save partial progress first.

## TTS (`providers/tts/`)
`TTSProvider.synthesize(text, out_path, voice=None) -> Path` writes an audio file (the provider's
`extension`, `.wav` for VieNeu-TTS) and returns it; `list_voices() -> list[str]` (may be empty).
Errors derive from `TTSError` (`TTSSetupError`, `TTSVoiceError`); there is no quota. The
synthesize stage turns a `TTSError` into a failure that names the line number (the job is
FAILED, finished clips are kept). In tests, never load the real package or use the network:
`VieNeuTTS(config=cfg, transport=fake)` where `fake.synthesize(text, voice, out_path)` writes a
file and `fake.list_voices()` returns `(label, voice_id)` pairs.

## ffmpeg (`utils/ffmpeg.py`)
`find_ffmpeg()`, `find_ffprobe()`, `run_ffmpeg(args, desc="")`, `probe_duration(path)`,
`has_audio_stream(path)`; failures raise `FFmpegError`.

## Gemini (`providers/gemini_client.py`)
`GeminiClient(model=..., requests_per_minute=..., transport=None)`;
`generate_text(prompt, audio_path=None, json_mode=False)` rate limits, retries 429/5xx with
backoff, raises `QuotaExhaustedError` on a daily quota. In tests, never use the network:

    fake = FakeGeminiTransport(['{"segments": []}', GeminiAPIError(503, "x"), "text"])
    client = GeminiClient(model="m", requests_per_minute=1000, transport=fake,
                          sleep=lambda s: None)
    # fake.prompts / fake.calls record what was sent; fake.queue(...) adds responses
