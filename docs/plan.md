# Implementation plan: Chinese -> Vietnamese video translator

Status: **proposal, no implementation code written yet.** Nothing below is built until you approve it.
Items marked **(verify)** are things I believe are true but will confirm in a short spike before relying on them.

## 1. Goal and constraints (as you told me)

- Input: a Chinese video. Output: the same video with the **Chinese speech replaced by a Vietnamese voice**, with the **background music and sound effects kept**, plus Vietnamese subtitles.
- Python, controlled from a **local web UI**.
- **Not fully offline**: cloud APIs are allowed for some steps.
- **NVIDIA GPU when available, CPU as fallback.**
- Your machine reports as Windows (ScottPC), so paths, ffmpeg install and GPU setup are planned for Windows first.

## 2. Pipeline recap

```
video -> [1 extract audio] -> [2 separate: vocals | background]
vocals -> [3 transcribe zh] -> [4 translate zh->vi] -> [5 synthesize vi] -> [6 align to time slots]
aligned voice + background -> [7 mix] -> new audio
vi segments -> [8 subtitles .srt / burn-in]
video + new audio (+ subs) -> [9 render] -> output.mp4
```

Every AI step (2, 3, 4, 5) sits behind a provider interface, so local and cloud options can be swapped by config.

## 3. Stage-by-stage plan

### 1. Extract audio
- **Tool:** ffmpeg/ffprobe (system install; the app checks for it at startup and tells you if missing).
- Output: 44.1 kHz stereo WAV for separation, plus a 16 kHz mono copy for speech recognition.
- Edge cases: videos with several audio tracks (pick the first, make it configurable), no audio track (clear error).

### 2. Separate vocals from background
- **Local (default):** Demucs (`htdemucs`, two-stem "vocals" mode). Good quality, runs on GPU; on CPU it works but is slow (several times real time) **(verify on your hardware)**.
- **Alternative local:** the `audio-separator` package (UVR/MDX models), sometimes cleaner on speech-over-music.
- **Cloud:** none planned at first; separation is heavy to upload and local quality is good enough.
- Risk: a little Chinese voice can remain in the "background" track. Mitigation: model choice is configurable; I will test on your real clips.
- GPU handling: pick device with `resolve_device`; process in chunks so long videos do not exhaust VRAM.

### 3. Transcribe Chinese speech
- **Local (default):** `faster-whisper` with `large-v3` (float16 on CUDA, int8 on CPU). Gives timestamps per segment.
- **Alternative local:** FunASR/Paraformer, often strong on Mandarin with built-in punctuation **(verify)**.
- **Cloud:** OpenAI transcription API or Google Speech-to-Text.
- Use voice activity detection so segments follow real speech boundaries (good boundaries matter for dubbing).
- Optional later: speaker diarization (pyannote; needs a free Hugging Face token) to give different voices to different speakers.
- Output: list of `Segment(start, end, text_zh)`.

### 4. Translate Chinese -> Vietnamese
- **Cloud (default recommendation):** a large LLM (Claude, GPT or Gemini via API). Best at natural Vietnamese and at following instructions like "keep it short enough to speak in N seconds".
- **Local option:** Qwen2.5 (via Ollama or llama.cpp) for Chinese-to-Vietnamese, or NLLB-200 **(verify Vietnamese quality)**. Lower quality than top cloud LLMs but free and private.
- Approach: translate in batches with neighbouring segments as context, a glossary for names/terms, and a **target length per segment** based on its duration.
- Output: `text_vi` on each segment.

### 5. Synthesize Vietnamese speech
- **Cloud options (most natural Vietnamese):** Azure Speech (`vi-VN` neural voices), Google Cloud TTS (`vi-VN`), ElevenLabs (multilingual, supports Vietnamese **(verify)**).
- **Local options:** Piper (Vietnamese voices exist, fast, CPU-friendly), Meta MMS-TTS Vietnamese (lower quality), community Vietnamese fine-tunes of F5-TTS **(verify)**.
- Note: XTTS-v2 does not officially list Vietnamese **(verify)**, so it is not the plan.
- One voice per speaker/gender, set in config. Voice cloning is out of scope unless you ask for it.
- Output: one audio clip per segment.

### 6. Align clips to time slots
- Problem: Vietnamese speech is often longer or shorter than the Chinese original.
- Strategy, in order: (a) ask the translation step for a length-appropriate sentence, (b) speed up/slow down the clip within a safe range (about 0.85x-1.3x) using ffmpeg `atempo` or rubberband, (c) let it borrow silence before/after the segment, (d) as a last resort trim or flag the segment for manual review.
- Segments that still do not fit are reported in the UI instead of silently degraded.

### 7. Mix
- Place aligned voice clips on a timeline, mix with the background track using ffmpeg/pydub, with configurable `voice_volume` and `background_volume`, then loudness-normalise (`loudnorm`).

### 8. Subtitles
- `.srt` writer from Vietnamese segments with line-length and reading-speed limits (split/wrap long lines).
- Burn-in via ffmpeg's subtitles filter (libass) with a font that supports Vietnamese diacritics (e.g. Noto Sans). Windows paths need careful escaping in ffmpeg filters, so this gets its own test.
- Both outputs are controlled by `subtitles.srt` and `subtitles.burn_in`.

### 9. Render
- No burn-in: copy the video stream (`-c:v copy`), fast and lossless; replace audio with the new mix.
- Burn-in: re-encode with `h264_nvenc` on NVIDIA, `libx264` on CPU.
- Output mp4 (AAC audio) plus the `.srt` next to it.

## 4. Cross-cutting design

- **GPU/CPU:** one `resolve_device()` decides; each provider maps it to its own setting (e.g. float16 vs int8). A `video-translator doctor` command reports ffmpeg, CUDA, VRAM and missing API keys before a job starts.
- **Caching and resume:** each stage writes its result into the job's work folder; re-running skips finished stages, so a failure at TTS does not redo separation.
- **Config and secrets:** choices in `config/default.yaml` (overridable per job), API keys only in `.env`, never committed.
- **Errors:** every stage fails with a clear message and leaves the work folder intact.
- **Review step (recommended):** pause after translation so you can edit the Vietnamese text in the UI before spending TTS credits.
- **Web UI:** FastAPI backend, a background worker, progress via polling or server-sent events; pages for upload, settings, progress, segment review/edit, and download.
- **Cost awareness:** show an estimate of cloud usage (characters/minutes) before running paid steps.

## 5. Order of work (milestones)

Each milestone ends with working tests and something you can run.

1. **M0 - Environment:** `doctor` command, ffmpeg detection, device resolution, config loading, Windows path handling.
2. **M1 - ffmpeg stages:** `extract_audio` and `render` working end to end (pass-through video with original audio).
3. **M2 - Transcribe + subtitles:** faster-whisper provider, `.srt` writer; you get Chinese subtitles from a real clip.
4. **M3 - Translate:** LLM translation provider (cloud first, local second); you get Vietnamese `.srt`.
5. **M4 - Voice:** TTS provider, `align`, `mix` (no separation yet, original audio removed): first Vietnamese dub.
6. **M5 - Separation:** Demucs provider; dub now keeps music and effects.
7. **M6 - Web UI:** upload, progress, review/edit, download.
8. **M7 - Polish:** diarization and per-speaker voices, caching, batch queue, packaging/installer notes.

Rationale: subtitles first gives early visible value and exercises transcription and translation; the hardest quality risk (timing of the dubbed voice) comes in M4 while it is still cheap to change course.

## 6. Testing approach

- **Unit tests** with fake providers (no GPU, no network): pipeline ordering, caching/resume, segment splitting, `.srt` formatting, alignment maths.
- **Provider contract tests:** every provider of a type must satisfy the same interface test, run against a fake and, optionally, the real service.
- **Sample fixtures:** a 10-20 second Chinese clip with speech over music (you supply one, or I generate a synthetic one with ffmpeg) for integration tests.
- **Markers:** `slow`, `gpu` and `cloud` tests are skipped by default; run on demand.
- **Manual listening checklist** for audio quality (voice volume, music kept, no leftover Chinese, sync).
- **Process:** I plan and review; Sonnet sub-agents write code, fix bugs and run tests per milestone and report back briefly. Code is delivered into `C:\video-translator` for you to commit.

## 7. Main risks

- Dubbed Vietnamese running longer than the original speech (alignment, section 3.6).
- Residual Chinese voice in the separated background.
- Vietnamese TTS quality and cost across providers.
- Windows + GPU setup friction (CUDA/PyTorch versions) - handled by `doctor` and install docs.
- Mistranslation of names and slang - handled by glossary and the review step.

## 8. Open questions for you (please answer, I will not guess)

1. **GPU:** which NVIDIA model and how much VRAM? (Decides Whisper/Demucs model sizes.)
2. **OS:** is Windows the only target, or should it also run on Mac/Linux?
3. **Cloud budget and keys:** which cloud providers do you already have keys for or prefer (Anthropic, OpenAI, Google, Azure, ElevenLabs)? Is a small per-video cost OK?
4. **Translation:** prefer best quality (cloud LLM) or privacy/free (local model) as the default?
5. **Voice:** prefer the most natural voice (cloud TTS) or free/local (Piper-class)? Male, female, or per-speaker? Any interest in voice cloning?
6. **Speakers:** are your videos mostly a single speaker, or multiple speakers/dialogue?
7. **Video length and volume:** typical length (minutes vs hours), and one at a time or batches?
8. **Subtitles:** default to `.srt` only, burned-in only, or both? Do you want Chinese and Vietnamese shown together (bilingual)?
9. **Review step:** do you want the pause-and-edit step after translation, or fully automatic?
10. **Content:** is it your own or licensed content? (Matters mainly if you later want voice cloning.)
11. **Python version** you have installed, and whether you are OK installing ffmpeg and (for GPU) the CUDA-enabled PyTorch.

## 9. Approval gate

No implementation code will be written until you approve this plan, ideally with answers (or "you decide") for the questions in section 8. You can also approve a single milestone at a time.
