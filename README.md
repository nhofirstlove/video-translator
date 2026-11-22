# video-translator

A locally run Python tool that takes a Chinese video (30 to 60 minutes is the target) and
produces a Vietnamese version: a Vietnamese dubbed voice plus a Vietnamese `.srt` subtitle
file. Version 1 uses **Gemini** (transcription and translation, free tier) and
**VieNeu-TTS** (Vietnamese voice: free, open source, runs on your own computer, no account and
no limits). It runs on Windows, CPU only, and is controlled from a small local web page or
from the command line.

## How it works

1. **Extract audio**: ffmpeg pulls the audio out of the video and cuts it into overlapping
   chunks (default 5 minutes).
2. **Transcribe**: a speech detector listens to the audio and finds each *stretch of speech*;
   every pause of 0.4 seconds or more starts a new line, and the line's start and end are the
   stretch's own start and end. A local speech model (faster-whisper) supplies the words of each
   stretch, and Gemini corrects the Chinese. So the timing comes from the sound, the words from
   Whisper and Gemini, and subtitles line up with the speech. Extra care: a **beep tone** (a
   bleeped swear word) is detected in the audio and gets its own line, and Gemini writes the
   censored word, which is then translated and spoken; filler sounds ("hmm", 嗯, 啊, laughter)
   never become lines; Gemini adds a line it hears that the detector missed.
3. **Translate**: Gemini translates the segments into spoken Vietnamese, in batches.
4. **Review**: the tool stops and writes a text file with the Vietnamese lines so you can fix
   mistakes. It continues only after you approve.
5. **Subtitles**: a Vietnamese `.srt` is written.
6. **Synthesize**: VieNeu-TTS speaks each Vietnamese line (one voice).
7. **Align**: every spoken clip is sped up, slowed down or trimmed to fit its time slot, and
   all clips are laid on one full-length voice track.
8. **Mix**: the voice track is mixed with the original audio, which is turned down
   ("ducked") while the voice speaks.
9. **Render**: ffmpeg puts the new audio under the original video (the video stream is
   copied, not re-encoded) and copies the `.srt` next to it.

Every step saves its progress, so a stopped, paused or failed job resumes without repeating
finished work. See [docs/architecture.md](docs/architecture.md) for details.

## Getting started

ffmpeg must be installed separately and available on your `PATH` (Windows:
`winget install Gyan.FFmpeg`, then reopen the terminal).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Copy `.env.example` to `.env` and add your key: `GEMINI_API_KEY` (from Google AI Studio).
The tool reads `.env` automatically at startup; keys are never printed or stored in job files.

### Speech timing model (faster-whisper)

It is installed together with the project (`pip install -e ".[dev]"`). To install or repair it
alone, with the virtual environment activated: `pip install faster-whisper`.

The **first transcription downloads the Whisper model** (`medium`, about 0.8 GB) from Hugging
Face, so internet is needed once and the first start takes a few minutes; afterwards it works
offline. It runs on the CPU, so expect roughly 20 to 40 extra minutes for an hour of video. To
make it faster at some cost in accuracy, put this in `config/local.yaml`:

```yaml
whisper:
  model: small      # tiny / base / small / medium / large-v3
```

Two more settings control the line timing: `whisper.pause_seconds` (default 0.4: a pause this
long starts a new line) and `whisper.vad_threshold` (default 0.3: lower hears quieter speech,
higher ignores more background noise).

To go back to Gemini-only timing (faster, but subtitles may drift by seconds), set
`providers: {asr: gemini}`.

### Install the voice (VieNeu-TTS)

The Vietnamese voice is a separate, free package. With the virtual environment **activated**
(you see `(.venv)` in the prompt), run:

```bash
pip install vieneu
```

Quick test (this should write `test.wav`, a short spoken sentence):

```bash
python -c "from vieneu import Vieneu; t=Vieneu(); a=t.infer('Xin chào, đây là giọng thử.'); t.save(a,'test.wav')"
```

The **first run downloads the voice model from Hugging Face**, so internet is needed once
(afterwards it works offline) and the first start can take a few minutes. On a CPU, speaking
one line takes a few seconds, so a long video takes a while; this is normal.

If you prefer the model to run as a separate local service (fallback), install `uv`, then:

```bash
git clone https://github.com/pnnbao97/VieNeu-TTS
cd VieNeu-TTS
uv sync
uv run python -m apps.openai_speech      # keep this window open; serves http://localhost:8000
```

and tell this tool to use it in `config/local.yaml`:

```yaml
vieneu:
  mode: server
```

## Using it

### 1. Check your setup

```bash
video-translator doctor
```

One line per check: `[OK]`, `[WARN]` (e.g. a key you have not added yet) or `[FAIL]` (must be
fixed; exit code 1). It checks Python 3.10+, `ffmpeg`/`ffprobe`, RAM and CPU, the Gemini key
(never shown), whether `faster-whisper` is installed, whether the voice package is installed (`vieneu`; in server mode whether the
server answers) and whether the config loads.

**"`video-translator` is not recognized"?** The command only exists while the virtual
environment is active and the project is installed. In PowerShell run
`.venv\Scripts\Activate.ps1` (you should see `(.venv)` in the prompt), then
`pip install -e ".[dev]"` again. As a fallback you can always run the same commands as
`python -m video_translator doctor`, `python -m video_translator serve`, and so on.

### 2. Choose the voice

List the preset voices and listen to one:

```bash
video-translator voices
video-translator say "Xin chào, đây là giọng thử." --voice "Mai Anh" --out sample.wav
```

`say` speaks one sentence with the configured voice (or `--voice`) and prints the file path
(default `say.wav`). Settings live in `config/default.yaml`; do not edit it. Create
`config/local.yaml` (git-ignored) containing only what you want to change:

```yaml
vieneu:
  voice: "Mai Anh"
```

Other `vieneu` settings: `mode` (`library` or `server`), `precision` (`fp32`, or `int8` which
is faster on CPU but needs a modern CPU with VNNI and can garble speech on older ones),
`server_url` and `timeout_seconds`. You can change the voice at any time; lines already
spoken keep their old voice, so change it before starting a job.

### 3. Run it: web page

```bash
video-translator serve
```

Open http://127.0.0.1:8000 (only reachable from this computer). Upload a video, watch the
progress, and when the job says "awaiting review" fix the Vietnamese lines in the page and
press Approve. When it is done, download the `.mp4` and the `.srt`. A paused job (for example the
Gemini daily quota) has a Resume button.

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

### The editor

On the web page, every video that already has lines (waiting for review, paused, failed or
finished) has an **Open editor** button (`/editor/<job id>`). The editor shows the video with
the Vietnamese subtitles on top, a timeline of all lines and one row per line:

- Click a line to jump the video to it. Edit the Vietnamese text and the start/end times
  (`mm:ss.mmm` or plain seconds); a bad time is refused with a message. Changes are saved on
  the server right away.
- **Listen** plays a line's voice clip. **Regenerate** (or **Generate voice** for a line with no
  voice yet) speaks that one line again at once. It is free and runs on your computer, so it
  may take a few seconds. A **Speed** slider (0.7 to 1.5) changes how fast a line is spoken
  without speaking it again; "Reset to auto" goes back to the automatic fit.
- **Dub preview** plays the clips over the video in the browser, with the original sound
  lowered while a voice speaks. It is only an approximation; the exact result is the
  re-rendered mp4.
- The volume sliders (voice, background, background while the voice speaks) are saved with the
  job. **Apply and re-render** rebuilds the subtitles (.srt), the voice track, the mix and the
  mp4 from your edits. It never speaks new lines; lines without a voice stay silent (the editor
  lists them). While it runs, the editor is read-only; when it finishes you can download the
  new files, or tick **Dubbed result** to watch the finished mp4.

- **Blur the Chinese subtitles**: tick the box in the "Blur the Chinese subtitles" section of the
  editor. A box appears over the video: drag it to move it, drag its edges or corners to resize
  it (or type the position and size in pixels of the original video). Set the blur strength,
  an optional dark layer and the Vietnamese text size. The Vietnamese subtitles are drawn white
  with a dark outline, centred in the box; the preview in the page is only an approximation.
  The settings are saved for that video right away; **Save as default for new videos** writes
  them to `config/local.yaml` (under `blur:`; comments in that file are not kept) and **Use the
  default again** drops the video's own choice. The box and the burned-in subtitles appear in
  the video after **Apply and re-render**. Only when the box is on is the video re-encoded
  (H.264, roughly the video's own length on a CPU); otherwise the video is copied unchanged.
  The box covers the whole video, so set it where the Chinese subtitles sit (usually the bottom
  centre). If the Vietnamese text is taller than the box, make the box taller or the text size
  smaller. This needs an ffmpeg build with `libass` (the Windows `Gyan.FFmpeg` build has it).

- **Tracks (timeline)**: under the video there is a timeline like in a video editor, with four
  tracks: the Vietnamese voice (each line is a block with its waveform), the background music
  (the original audio with its waveform; there is no music/voice separation yet, so it also
  contains the Chinese speech), the subtitles, and the blur (one bar for the whole video;
  click it to jump to the blur settings). Click a block to select that line. **Drag a block**
  to move the line in time and **drag its left or right edge** to change its start or end
  (the same rules as the time boxes: no overlap with the next or previous line, at least 0.2 s;
  it snaps to neighbouring lines and the playhead; `Esc` while dragging cancels). The voice and
  subtitle blocks are the same line, so they move together. After moving a line, press **Apply
  and re-render** so the voice is fitted to the new time. Zoom with the `-` / `+` buttons, the
  slider, **Fit** or `Ctrl` + mouse wheel. Each track has a **Mute** (voice, music) or **Hide**
  (subtitles, blur) button; these only change what you see and hear while editing in the
  browser and never change the rendered video.

Keyboard shortcuts: `Space` play/pause (when not typing), `Alt+Up` / `Alt+Down` previous/next
line, `Ctrl+Enter` in a line saves and moves to the next line, `[` / `]` set the start / end of
the active line to the current video time.

### Where files go

| What | Where |
| --- | --- |
| Source videos (web uploads) | `data/input/` |
| Per-job work files, resumable state (`job.json`, chunks, transcripts, translations, review file, voice clips, mixed audio) | `data/work/<job id>/` |
| Finished video and subtitles | `data/output/<name>.vi.mp4` and `data/output/<name>.vi.srt` |

### Limits

- **VieNeu-TTS** has no quota or cost, but it runs on your CPU: expect a few seconds per
  line, so a 30 to 60 minute video can take a long time to voice. **Try a short clip first.**
  If a line fails, the job stops with a message naming the line (finished clips are kept);
  fix the cause and resume.
- **Gemini** free tier: strict per-minute and per-day request limits. Long videos are slow
  (the tool waits to stay under `gemini.requests_per_minute`) and may hit the daily quota.
  Then the job pauses (status "paused"); run `resume` the next day and it continues from the
  last finished chunk or batch.

### What version 1 does not do

- **No music/voice separation.** The original audio is ducked (lowered to
  `audio.ducked_volume`, default 25%) under the Vietnamese voice, so the Chinese voice is
  still faintly audible, and so is the music and effects at the lowered level. (Bleep tones of
  censored words are the exception: they are silenced completely.)
- **Burned-in subtitles only with the blur box.** Otherwise only a separate `.srt` is written
  (`subtitles.burn_in` is ignored with a warning); turn the blur box on in the editor to burn
  the Vietnamese subtitles into the video.
- One speaker/voice for the whole video; no per-speaker voices or voice cloning.
- Gemini is still a cloud service; the voice is local (CPU), with no GPU tuning.

### Troubleshooting transcription

- **"The faster-whisper package is not installed"**: activate the virtual environment and run
  `pip install faster-whisper`.
- **"Could not load the Whisper model"**: the first run downloads it from Hugging Face; connect
  to the internet once and try again (or choose a smaller `whisper.model`).
- **Old videos still have shifted subtitles**: transcripts made before this change are not
  reused; upload the video again to get a new job.
- **A bleeped word is missing or wrong**: the censored word is a guess by Gemini from what it
  can hear under the beep; fix it in the review step or the editor. To turn the beep handling
  off, use `whisper: {detect_beeps: false}`.
- **A real line is still missing** (quiet speech under loud music): lower
  `whisper: {vad_threshold: 0.2}`; lines Gemini hears in a gap are added automatically, and the
  job message tells how many. Separating the voice from the music is not built in yet.
- **Lines are cut too often or too rarely**: change `whisper: {pause_seconds: 0.6}` (longer
  pause needed) or `0.3` (shorter pause is enough).
- **The beep is still audible in the dub**: the original bleep tone is silenced in the final
  audio (`audio: {remove_beeps: true}`, the default); set it to `false` to keep it.

### Troubleshooting the voice

- **"The VieNeu-TTS voice package is not installed"**: activate the virtual environment and run
  `pip install vieneu` (check with `video-translator doctor`).
- **"Could not load the VieNeu-TTS voice model"**: the first run downloads the model from
  Hugging Face; connect to the internet once and try again.
- **"Cannot reach the VieNeu server"**: you set `vieneu: {mode: server}` but the server is not
  running. Start it (`uv run python -m apps.openai_speech` in the VieNeu-TTS folder) or remove
  that setting.
- **Garbled or noisy speech**: set `vieneu: {precision: fp32}` (the default); `int8` can garble
  on older CPUs.
- **"Unknown VieNeu voice"**: run `video-translator voices` and copy a name exactly.
- **A job failed on line N of the voice step**: fix the text of that line (editor or review
  file) if it is unusual, then `resume`; clips already made are kept.

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
    cli.py                 `video-translator run | resume | serve | doctor | voices | say`
    config.py              pydantic settings model + YAML loader
    models.py              Segment, Chunk and Job models shared by all stages
    pipeline.py            runs the stages in order; resumable
    review_file.py         the human-editable review file
    doctor.py              setup checks
    stages/                one module per step (extract_audio ... render)
    providers/             gemini_client, registry, asr/, translation/, tts/ (Gemini, VieNeu-TTS)
    utils/                 ffmpeg helpers, srt writer, device selection
    synth_line.py          speak one line (editor): one TTS call, then re-fit the clip
    web/                   FastAPI app, job manager, editor API, HTML pages and static files
tests/                     unit tests plus end-to-end tests with fake Gemini/VieNeu
```

## Design decisions

- **Providers are swappable.** Each AI step sits behind a small interface and is chosen by
  name in the config (`providers.asr`, `providers.translation`, `providers.tts`).
- **Stages share a Job and a per-job work dir.** Steps are independent, resumable and easy
  to test; the job is saved after every stage.
- **Never repeat work.** Finished chunks, translation batches and voice clips are kept on
  disk and skipped on resume.

## Status and roadmap

Version 1 is implemented and covered by end-to-end tests (real ffmpeg and real stages, fake
Gemini and VieNeu): extract audio, transcribe, translate, review, subtitles,
synthesize, align, mix, render, plus the web page, the CLI and `doctor`. It has not yet been
tried on a real 30 to 60 minute video with the real Gemini key and the real VieNeu model;
expect to tune the Gemini model name in `config/local.yaml` (`gemini.model`) and the voice.

Planned for version 2:

- [ ] Separate vocals from music/effects (Demucs) so the Chinese voice disappears and the
      background stays at full level (`SeparateAudioStage` is reserved for this)
- [ ] Local transcription/translation models and GPU use
- [ ] More than one speaker/voice
- [ ] Other translation/TTS providers
