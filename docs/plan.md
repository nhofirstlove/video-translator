# Implementation plan (revision 3, simplified): Chinese -> Vietnamese video translator

Status: **proposal, no implementation code written yet.** Nothing is built until you say go.
Items marked **(verify)** are things I believe are true but will confirm in a short test before relying on them.

## 1. Decisions so far

- Python, local web UI, Windows, no NVIDIA GPU (CPU only is fine).
- Videos 30-60 minutes, your own content, Vietnamese **`.srt` file** as subtitle output (no burn-in).
- **Version 1 (keep it simple): Gemini for all language/AI-understanding work, ElevenLabs for the Vietnamese voice.** Nothing else.
- **Pause-and-edit:** after translation the tool stops so you can fix the Vietnamese line by line before any voice is made.
- Faint Chinese voice under the dub is accepted in v1. Music and effects also get quieter while the Vietnamese voice speaks (ducking).
- **Later (not in v1):** 9router fallback, free/local TTS, local Whisper, Demucs separation (removes the Chinese voice fully and keeps effects at full volume), speaker detection, burn-in subtitles, batch queue.

## 2. Version 1 pipeline

```
video -> [1 extract audio, cut into ~10-min chunks]
      -> [2 transcribe zh (Gemini)]
      -> [3 translate zh->vi (Gemini)]
      -> [4 PAUSE: you review/edit the Vietnamese]
      -> [5 synthesize vi voice (ElevenLabs)]
      -> [6 fit each clip to its time slot]
      -> [7 mix: Vietnamese voice over the ducked original audio]
      -> [8 write .srt]
      -> [9 render: original video + new audio -> mp4]
```

Each step saves its result in a job folder, so a failure or a rate limit pauses the job and it can resume.

## 3. Step details

1. **Extract audio:** ffmpeg. Chunks of about 10 minutes (small overlap) keep uploads and memory small and make resume possible.
2. **Transcribe (Gemini):** send each audio chunk to Gemini and ask for a timestamped Chinese transcript. This keeps v1 to a single cloud key and no local models. **Risk (verify):** Gemini's timestamps may drift, which would hurt dub timing. The first test (E2) checks this on a real clip; if it is not accurate enough I will come back to you with options (for example a small local Whisper model) before changing anything.
3. **Translate (Gemini, free tier):** batches of segments with neighbouring lines as context and a target length per line so the Vietnamese fits the time slot. Works with free or paid Gemini keys: rate-limited, retried with backoff, progress saved per batch so a daily limit pauses and later resumes.
4. **Review gate:** the job stops at "awaiting review". You see each line (time, Chinese, Vietnamese), edit the Vietnamese and click Continue (web UI), or edit a file and run `video-translator resume` (command line, before the UI exists). Switchable off in config.
5. **Voice (ElevenLabs):** one clip per segment using a default Vietnamese voice stored in `config/default.yaml` (`tts.voice`), changeable later. **Important (verify):** ElevenLabs' free tier gives only roughly 10,000 characters a month, about ten minutes of speech, so it **will not cover a 30-60 minute video**; a paid plan will be needed for full-length videos. The tool shows the character count and estimated usage before sending anything to ElevenLabs, and you can test on short clips on the free tier.
6. **Fit to time slots:** Vietnamese is often longer or shorter than the Chinese. Clips are sped up or slowed within a safe range (about 0.85x-1.3x) with ffmpeg; anything still too long is flagged in the results instead of silently cut.
7. **Mix:** the original audio is lowered while each Vietnamese clip plays and returns to normal volume in the gaps, then loudness-normalised. Volumes are configurable.
8. **Subtitles:** `.srt` from the (edited) Vietnamese text, with line-length and reading-speed rules.
9. **Render:** copy the video stream untouched (no slow re-encoding), attach the new AAC audio. Output `mp4` plus the `.srt`.

## 4. Order of work (version 1)

1. **E0:** setup check (`doctor`: Python, ffmpeg, keys present), config loading, Windows paths. README install steps already written.
2. **E1:** ffmpeg stages: extract + chunk audio and render. Runs end to end with the original audio.
3. **E2:** Gemini transcription plus `.srt` writer; includes the timestamp-accuracy test. You get Chinese subtitles from a real clip.
4. **E3:** Gemini translation plus the review gate (command-line pause, edit, resume). You get a corrected Vietnamese `.srt`. **First useful result.**
5. **E4:** ElevenLabs voice, time-slot fitting, ducked mix: first Vietnamese dub (try it on a short clip first).
6. **E5:** web UI: upload, progress, review/edit table, download; then a full 30-60 minute test.

## 5. Testing

- Unit tests with fake Gemini/ElevenLabs providers (no network, no cost): step order, resume, chunk merging, `.srt` format, fitting maths.
- Real-service tests are opt-in only, so free-tier quota is not burned by accident.
- A short Chinese sample clip with speech over music (you supply one, or I generate a synthetic one).
- Timing and cost measured per step on your PC.
- Manual listening checklist: sync, voice volume, music level, leftover Chinese.
- Process: I plan and review; Sonnet sub-agents write code, fix bugs and run tests and report back briefly. Files go into `C:\video-translator` for you to commit.

## 6. Open questions

Answered: Gemini does the transcription too (with the E2 timestamp test as a safety check); **both Gemini and ElevenLabs are on the free tier**; **one speaker**.

1. **Free-tier consequence (needs your decision before E4):** with ElevenLabs free (~10,000 characters/month **(verify)**), a full 30-60 minute video cannot be voiced in one month. Options: (a) build v1 as designed and use it on short clips/excerpts now, upgrading ElevenLabs later for full videos; (b) add a "voice only the first N minutes / selected lines" option so you can spend the free quota where you choose; (c) swap in a free voice model (this was going to be "later"). Which do you prefer? My suggestion: (a) plus the usage counter, and (b) if cheap to add.
2. **Default voice:** you did not say male or female. I will pick one default Vietnamese ElevenLabs voice for the first test (suggestion: female, neutral narration style) and you can change it in `config/default.yaml`. Tell me if you prefer male.
3. **PC (optional):** CPU and RAM? Less important now since the heavy work is in the cloud.

## 7. Approval gate

No implementation code is written until you explicitly say so, for example "start E0 and E1". You can approve one milestone at a time.
