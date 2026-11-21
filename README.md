# video-translator

A locally run Python tool that takes a Chinese video and produces a Vietnamese video.
The Vietnamese dubbed voice replaces only the Chinese speech; background music and sound
effects are kept. Vietnamese subtitles are produced as a separate `.srt` file, burned into
the video, or both (set by config flags). It is controlled from a small local web UI.

## How it works

1. **Extract audio**: ffmpeg pulls the audio track out of the video.
2. **Separate audio**: the audio is split into vocals and background (music and effects).
3. **Transcribe**: the Chinese speech is turned into timed text segments.
4. **Translate**: each Chinese segment is translated into Vietnamese.
5. **Synthesize**: each Vietnamese segment is spoken by a text-to-speech voice.
6. **Align**: every spoken clip is stretched, trimmed or padded to fit its time slot.
7. **Mix**: the Vietnamese voice track is mixed with the untouched background track.
8. **Subtitles**: a Vietnamese `.srt` (and optionally a styled burn-in file) is built.
9. **Render**: ffmpeg combines the original video, the new audio and optional subtitles
   into the final mp4.

See [docs/architecture.md](docs/architecture.md) for the diagram and details.

## Folder structure

```
README.md                  this file
pyproject.toml             package metadata, dependencies, extras, console script
.env.example               template for cloud API keys (copy to .env)
config/default.yaml        default settings: device, providers, subtitles, audio, paths
docs/architecture.md       pipeline diagram, data flow, how to extend, work-dir layout
data/input/                put source videos here (contents git-ignored)
data/work/                 per-job intermediate files (contents git-ignored)
data/output/               finished videos and subtitles (contents git-ignored)
src/video_translator/
    cli.py                 `video-translator run` / `serve` commands
    config.py              pydantic settings model + YAML loader
    models.py              Segment and Job models shared by all stages
    pipeline.py            runs the stages in order and reports progress
    stages/                one module per pipeline step (extract_audio ... render)
        base.py            abstract Stage class
    providers/             swappable local/cloud implementations of AI steps
        registry.py        name -> class registry
        separation/        vocals/background separation (Demucs placeholder)
        asr/               speech recognition (faster-whisper placeholder)
        translation/       zh -> vi translation (LLM placeholder)
        tts/               text-to-speech (placeholder)
    utils/
        ffmpeg.py          ffmpeg/ffprobe helpers
        device.py          GPU/CPU selection
    web/                   FastAPI app, HTML template, static files
tests/                     tests for models, registry, pipeline, imports
```

## Design decisions

- **Providers are swappable.** Every AI step sits behind a small interface and is chosen by
  name in the config, so a local model or a cloud API can be exchanged without touching
  the pipeline.
- **Stages share a Job and a per-job work dir.** Each stage reads from and writes to one
  `Job` object and its own folder, so steps are independent and easy to re-run or test.
- **GPU with CPU fallback.** `device: auto` uses an NVIDIA GPU (CUDA) when available and
  falls back to the CPU otherwise.
- **Background is kept by separating vocals.** Instead of replacing the whole audio, the
  speech is isolated, so only the Chinese voice is swapped for the Vietnamese one.

## Status

Skeleton only. Nothing is functional yet beyond the models, the provider registry, the
pipeline wiring, device selection and the web `/health` endpoint. All stages and providers
raise `NotImplementedError`.

## Roadmap

- [ ] Extract audio
- [ ] Separate audio (vocals / background)
- [ ] Transcribe (Chinese ASR)
- [ ] Translate (Chinese to Vietnamese)
- [ ] Synthesize (Vietnamese TTS)
- [ ] Align (fit clips to time slots)
- [ ] Mix (voice + background)
- [ ] Subtitles (.srt and burn-in)
- [ ] Render (final mp4)
- [ ] Web UI (upload, progress, download)

## Getting started

ffmpeg must be installed separately and available on your `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Optional extras for the AI steps: `asr`, `separation`, `translation`, `tts`
(for example `pip install -e ".[asr,separation]"`). Copy `.env.example` to `.env` if you use
cloud providers.
