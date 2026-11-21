# Architecture

## Pipeline

```
 input.mp4
    |
    v
[1 extract_audio] --> audio.wav
    |
    v
[2 separate_audio] --> vocals.wav + background.wav
    |                        |
    v                        |
[3 transcribe]  (zh segments)|
    |                        |
    v                        |
[4 translate]   (vi segments)|
    |                        |
    v                        |
[5 synthesize]  (clip per segment)
    |                        |
    v                        |
[6 align]       (fit clips to time slots)
    |                        |
    v                        v
[7 mix] <---- voice track + background.wav --> mixed_audio.wav
    |
[8 subtitles] --> subtitles.srt (+ burn-in file)
    |
    v
[9 render] input.mp4 + mixed_audio.wav (+ subs) --> output.mp4
```

## Data flow

All stages share one `Job` object (`models.py`). Each stage receives the job and the
`AppConfig`, does its work, and returns the job:

- `job.segments` holds timed `Segment`s. `transcribe` creates them (`text_zh`),
  `translate` fills `text_vi`, `synthesize` sets `audio_path`, `align` rewrites `audio_path`.
- `job.artifacts` maps names (`audio`, `vocals`, `background`, `mixed_audio`, `srt`,
  `output`, ...) to files in the job's work dir.
- `Pipeline.run(job, on_progress)` runs the stages in order, reports progress as
  `(stage_name, completed, total)`, and marks the job `FAILED` if a stage raises.

AI steps (separation, ASR, translation, TTS) never hard-code a model. A stage asks the
provider registry for the class named in `config.providers.<kind>.name`, so local and
cloud implementations are interchangeable.

## Adding a stage

1. Create `stages/<name>.py` with a `Stage` subclass (`name`, `run(job, config) -> Job`).
2. Add the class to `ALL_STAGES` in `stages/__init__.py` at the right position.
3. Update `tests/test_imports.py` (expected order).

## Adding a provider

1. Pick the area (`separation`, `asr`, `translation`, `tts`) and subclass its `base.py` interface.
2. Decorate the class: `@register("<area>", "<name>")`.
3. Import the module in the area's `__init__.py` so it registers on import.
4. Select it in `config/default.yaml` under `providers.<area>.name`.
5. Add optional third-party dependencies as an extra in `pyproject.toml`.

## Work-dir layout

Each job gets its own folder under `paths.work_dir` (default `data/work/`):

```
data/work/<job-id>/
    audio.wav            extract_audio
    vocals.wav           separate_audio
    background.wav       separate_audio
    tts/                 synthesize (one clip per segment)
    aligned/             align
    mixed_audio.wav      mix
    subtitles.srt        subtitles
    subtitles.ass        subtitles (burn-in, optional)
```

The final video is written to `paths.output_dir` (default `data/output/`).
