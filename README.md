# video-translator

A locally run Python tool that takes a Chinese video (30 to 60 minutes is the target) and
produces a Vietnamese version: a Vietnamese dubbed voice plus a Vietnamese `.srt` subtitle
file. Version 1 uses **Gemini** (transcription and translation) and **ElevenLabs**
(Vietnamese voice), both on their free tiers. It runs on Windows, CPU only, and is controlled
from a small local web page or from the command line.

## How it works

1. **Extract audio**: ffmpeg pulls the audio out of the video and cuts it into overlapping
   chunks (default 10 minutes).
2. **Transcribe**: Gemini turns each chunk into timed Chinese text segments.
3. **Translate**: Gemini translates the segments into spoken Vietnamese, in batches.
4. **Review**: the tool stops and writes a text file with the Vietnamese lines so you can fix
   mistakes. It continues only after you approve.
5. **Subtitles**: a Vietnamese `.srt` is written.
6. **Synthesize**: ElevenLabs speaks each Vietnamese line (one voice).
7. **Align**: every spoken clip is sped up, slowed down or trimmed to fit its time slot, and
   all clips are laid on one full-length voice track.
8. **Mix**: the voice track is mixed with the original audio, which is turned down
   ("ducked") while the voice speaks.
9. **Render**: ffmpeg puts the new audio under the original video (the video stream is
   copied, not re-encoded) and copies the `.srt` next to it.

Every step saves its progress, so a stopped, paused or failed job resumes without repeating
finished (and paid) work. See [docs/architecture.md](docs/architecture.md) for details.

## Getting started

ffmpeg must be installed separately and available on your `PATH` (Windows:
`winget install Gyan.FFmpeg`, then reopen the terminal).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Copy `.env.example` to `.env` and add your keys: `GEMINI_API_KEY` (from Google AI Studio)
and `ELEVENLABS_API_KEY`. The tool reads `.env` automatically at startup; keys are never
printed or stored in job files.

## Using it

### 1. Check your setup

```bash
video-translator doctor
```

One line per check: `[OK]`, `[WARN]` (e.g. a key you have not added yet) or `[FAIL]` (must be
fixed; exit code 1). It checks Python 3.10+, `ffmpeg`/`ffprobe`, RAM and CPU, the API keys
(never shown) and whether the config loads.

### 2. Choose the ElevenLabs voice

Settings live in `config/default.yaml`; do not edit it. Create `config/local.yaml`
(git-ignored) containing only what you want to change:

```yaml
elevenlabs:
  voice: "PASTE_YOUR_VOICE_ID_HERE"
```

To get a voice id: open the ElevenLabs voice library
(https://elevenlabs.io/app/voice-library), filter by Language = Vietnamese, press
"Add to my voices" on a voice you like, then copy its voice id (the "ID" button next to the
voice). You can change the voice at any time; lines already spoken keep their old voice, so
change it before starting a job.

### 3. Run it: web page

```bash
video-translator serve
```

Open http://127.0.0.1:8000 (only reachable from this computer). Upload a video, watch the
progress, and when the job says "awaiting review" fix the Vietnamese lines in the page and
press Approve. When it is done, download the `.mp4` and the `.srt`. A paused job (quota used
up) has a Resume button.

### 4. Run it: command line

```bash
video-translator run "data/input/my video.mp4"
```

It prints the job folder and stops at the review step with a ready-made command:

```bash
video-translator resume data/work/<job id> --approve
```

`resume` without `--approve` continues a paused or failed job (it prints the exact command
for each case). Exit code 0 means "ran to a stop or finished" (a pause is not an error); 1
means a real failure (the message says which stage and why).

### The review step

Version 1 always stops after translation (`review: true` in the config). The file is
`data/work/<job id>/review/review.txt` (UTF-8, opens in Notepad):

```
#12  00:01:02,500 --> 00:01:05,100
ZH: 中文原文
VI: Bản dịch tiếng Việt
```

Edit only the `VI:` lines; do not change the `#number`/time line or the `ZH:` line, and do not
leave a `VI:` empty. Save, then approve (`resume ... --approve`). If the file is malformed the
job stops with a message naming the line. In the web page you edit the same lines in the
browser instead. Set `review: false` in `config/local.yaml` to skip the stop.

### Where files go

| What | Where |
| --- | --- |
| Source videos (web uploads) | `data/input/` |
| Per-job work files, resumable state (`job.json`, chunks, transcripts, translations, review file, voice clips, mixed audio) | `data/work/<job id>/` |
| Finished video and subtitles | `data/output/<name>.vi.mp4` and `data/output/<name>.vi.srt` |
| ElevenLabs monthly character counter | `data/usage/elevenlabs_YYYY-MM.json` |

### Free-tier limits

- **ElevenLabs** free tier: about 10,000 characters per month (the default
  `elevenlabs.monthly_character_budget`; change it in `config/local.yaml` if your plan
  differs). Spoken Vietnamese is roughly 15 characters per second, so 10,000 characters is
  only about 10 to 12 minutes of speech: **try short clips first**. Before any request the
  tool checks that the whole video fits in what is left this month; if not, the job pauses
  with a clear message and nothing is synthesized. Raise the budget (paid plan) or wait for
  next month, then resume.
- **Gemini** free tier: strict per-minute and per-day request limits. Long videos are slow
  (the tool waits to stay under `gemini.requests_per_minute`) and may hit the daily quota.
  Then the job pauses (status "paused"); run `resume` the next day and it continues from the
  last finished chunk or batch.

### What version 1 does not do

- **No music/voice separation.** The original audio is ducked (lowered to
  `audio.ducked_volume`, default 25%) under the Vietnamese voice, so the Chinese voice is
  still faintly audible, and so is the music and effects at the lowered level.
- **No burn-in subtitles.** Only a separate `.srt` is written (`subtitles.burn_in` is ignored
  with a warning).
- One speaker/voice for the whole video; no per-speaker voices or voice cloning.
- Only Gemini and ElevenLabs; no local models and no GPU use.

## Folder structure

```
README.md                  this file
pyproject.toml             package metadata, dependencies, console script
.env.example               template for API keys (copy to .env)
config/default.yaml        default settings; put your changes in config/local.yaml
docs/architecture.md       pipeline diagram, data flow, how to extend, work-dir layout
docs/contracts.md          artifact names, job fields, pause rules shared by all stages
data/input|work|output/    videos in, per-job files, finished files (contents git-ignored)
src/video_translator/
    cli.py                 `video-translator run | resume | serve | doctor`
    config.py              pydantic settings model + YAML loader
    models.py              Segment, Chunk and Job models shared by all stages
    pipeline.py            runs the stages in order; resumable
    review_file.py         the human-editable review file
    usage.py               monthly ElevenLabs character counter
    doctor.py              setup checks
    stages/                one module per step (extract_audio ... render)
    providers/             gemini_client, registry, asr/, translation/, tts/ (Gemini, ElevenLabs)
    utils/                 ffmpeg helpers, srt writer, device selection
    web/                   FastAPI app, job manager, HTML page and static files
tests/                     unit tests plus end-to-end tests with fake Gemini/ElevenLabs
```

## Design decisions

- **Providers are swappable.** Each AI step sits behind a small interface and is chosen by
  name in the config (`providers.asr`, `providers.translation`, `providers.tts`).
- **Stages share a Job and a per-job work dir.** Steps are independent, resumable and easy
  to test; the job is saved after every stage.
- **Never pay twice.** Finished chunks, translation batches and voice clips are kept on
  disk and skipped on resume; the ElevenLabs budget is checked before any request.

## Status and roadmap

Version 1 is implemented and covered by end-to-end tests (real ffmpeg and real stages, fake
Gemini and ElevenLabs): extract audio, transcribe, translate, review, subtitles,
synthesize, align, mix, render, plus the web page, the CLI and `doctor`. It has not yet been
tried on a real 30 to 60 minute video with real API keys; expect to tune the Gemini model
name in `config/local.yaml` (`gemini.model`) and the voice.

Planned for version 2:

- [ ] Separate vocals from music/effects (Demucs) so the Chinese voice disappears and the
      background stays at full level (`SeparateAudioStage` is reserved for this)
- [ ] Burn subtitles into the video
- [ ] Local models (faster-whisper, local TTS) and GPU use
- [ ] More than one speaker/voice
- [ ] Other translation/TTS providers
