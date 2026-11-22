# Editor UI: shared contract (U1 backend, U2 page/player/lines, U3 audio controls)

All paths are relative to the web app. `{id}` is a job id (`^[0-9a-f]{12}$`). Times are seconds (float) on the wire;
the UI shows `mm:ss.mmm` (`h:mm:ss.mmm` from one hour) and also accepts plain seconds like `75.4` when typing.
JSON errors: `{"detail": "<plain-language message>"}` with a 4xx status. Nothing here may expose API keys.

## Page
- `GET /editor/{id}` -> `web/templates/editor.html` (static HTML shell; JS reads the job id from `location.pathname`).
- Scripts load in this order: `/static/editor.js` (U2) then `/static/editor_audio.js` (U3). Styles: `/static/editor.css` (U2 owns; U3 may add `/static/editor_audio.css`).

## Data API
- `GET /api/jobs/{id}/editor` ->
  ```json
  {"job": {"id","status","message","current_stage","error","input_name"},
   "duration": 3601.2,
   "media": {"video": true, "dub_video": false, "voice_track": false},
   "segments": [{"index":0,"start":1.2,"end":4.0,"text_zh":"..","text_vi":"..","flags":["too_long"],
                 "has_audio":true,"audio_stale":false,"speed":null,"chars":42}],
   "audio": {"voice_volume":1.0,"background_volume":1.0,"ducked_volume":0.25},
   "budget": null}
  ```
  `budget` is always null (kept for compatibility: voice generation is free). `chars` = number of characters of `text_vi`.
  `speed` is null unless the user set one. Editing is only allowed when `job.status` is `awaiting_review`, `done`, `paused` or `failed`; a running job returns 409 on any edit endpoint.
- `PATCH /api/jobs/{id}/segments/{i}` body `{"start"?, "end"?, "text_vi"?}` -> the updated segment object (same shape as above).
  Validation: `0 <= start < end <= duration`, `end - start >= 0.2`, `text_vi` non-empty after trim, and start/end may not cross neighbouring lines by more than 0.05 s (overlap -> 422 with a clear message). If `text_vi` changed and a clip exists the segment gets `audio_stale=true`. Changing only times keeps audio but marks `flags` "needs_realign" (cleared on Apply).
- `GET /api/jobs/{id}/segments/{i}/audio` -> the line's voice clip (aligned clip if present else raw TTS clip), with HTTP Range support, `Cache-Control: no-store`; 404 `{"detail":"no voice yet"}` if none.
- `GET /api/jobs/{id}/segments/{i}/cost` -> `{"chars":42,"remaining":null,"allowed":true,"message":"Free: runs on your computer; may take a few seconds."}` (kept for compatibility; the page no longer calls it).
- `POST /api/jobs/{id}/segments/{i}/regenerate` (no body needed; `{"confirm":true}` is still accepted) -> updated segment. Runs at once: speaks this one line with VieNeu-TTS (free, local), clears `audio_stale`, re-fits the clip to its time slot (aligned clip). Never returns 402. Errors: 400 for an unknown voice, 502 with a plain message when the voice engine is missing, unreachable or fails (nothing changes).
- `POST /api/jobs/{id}/segments/{i}/speed` body `{"speed": 1.15}` (0.7 to 1.5) or `{"speed": null}` (clear the user speed: the line is fitted automatically again) -> updated segment. No new voice is made: re-creates the aligned clip from the raw clip with ffmpeg tempo change. 409 if the line has no raw clip.
- `PUT /api/jobs/{id}/audio-settings` body `{"voice_volume","background_volume","ducked_volume"}` (each 0.0 to 2.0; ducked <= background) -> same object. Stored on the job and used by the mix stage instead of the config values.
- `POST /api/jobs/{id}/apply` -> `{"started":true,"missing_audio":[3,17]}`. Rebuilds without any paid call: subtitles (.srt), align, mix, render, using the current segments/volumes. Lines without a clip stay silent (their indexes are returned in `missing_audio` but the run still starts). Progress is read from `GET /api/jobs/{id}` (existing endpoint: status, current_stage, message). If a job is already running -> 409.

## Blur box (U4, `editor_blur.js` / `editor_blur.css`)
- `GET /api/jobs/{id}/editor` also returns `"blur": {"settings": {enabled,x,y,width,height,strength,tint,font_size,font},
  "defaults": {...}, "video": {"width","height"}|null, "customised": bool}`. Pixels are pixels of the original video;
  `x`/`y` are resolved to the real box when the video size is known.
- `PUT /api/jobs/{id}/blur-settings` body `{enabled,x,y,width,height,strength(1-100),tint(0-0.9),font_size(8-300)}` -> same `blur` block
  (box clamped inside the picture, values stored in `job.blur_overrides`; 422 for bad values, 409 while running).
- `DELETE /api/jobs/{id}/blur-settings` -> forget this video's choice (it follows `config.blur` again).
- `PUT /api/blur-defaults` same body -> `{"defaults": {...}}`; merged into `config/local.yaml` under `blur:` and into the live config.
- The render stage uses the effective settings (`config.blur` + `job.blur_overrides`): enabled -> blur box + burned Vietnamese subtitles
  (ASS file `work/subtitles/blur.ass`), video re-encoded with libx264; disabled -> video stream copied.
- Frontend: `editor_blur.js` mounts `<div id="blur-panel">` and a `.eb-box` over `#ed-stage` on `Editor` 'ready'.

## Waveforms and the timeline (`editor_timeline.js` / `editor_timeline.css`)
- `GET /api/jobs/{id}/waveforms` -> `{"voice_rate": 50, "voice": {"3": {"duration": 2.4, "peaks": [0..100, ...]}, ...},
  "background_rate": 20, "background": {"duration": 3601.2, "peaks": [...]} | null}`. One entry in `voice` per line that has a clip
  (keyed by line index; the clip sits at the line's `start`); `background` is the original audio (music and speech together,
  there is no separation yet). `?index=N` returns only `voice` for that line (no background). Peaks are the loudest sample per
  slice, 0 to 100. 404 for an unknown line.
- Frontend: `editor_timeline.js` mounts `<div id="tracks-panel">` under the video on `Editor` 'ready': tracks Voice, Background
  music, Subtitles, Blur; zoom; per-track mute (voice, music) or hide (subtitles, blur) that only affects the preview in the
  browser, never the render; click a block to select that line; drag a subtitle or voice block to move the line, drag its edges
  to change start or end (same PATCH as the time fields: both tracks show the same line). Extra `Editor` members used:
  `seek(t)`, `select(i)`, `setTimes(i, start, end)`, `isEditable()`, `activeIndex()`, event 'active-changed' (index);
  `EditorAudio.setMute({voice, bg})` mutes the voice preview or the original audio in the browser.

## Media (all with HTTP Range support so seeking works)
- `GET /api/jobs/{id}/media/video` -> the original input video.
- `GET /api/jobs/{id}/media/dub` -> the finished dubbed mp4 (`artifacts["output"]`), 404 if not finished.
- `GET /api/jobs/{id}/media/voice` -> `artifacts["voice_track"]` wav if it exists.
Only files inside the job/work/input/output folders are ever served.

## Frontend contract between U2 and U3 (both are plain browser JS, no build step, no CDN)
U2 (`editor.js`) creates one global `window.Editor`:
```js
Editor.jobId            // string
Editor.state            // { job, duration, media, segments, audio, budget }  (latest server data; mutated in place)
Editor.video            // the <video> element (original video; plays its own audio)
Editor.api(method, path, body) -> Promise<json>   // fetch helper, throws Error(detail) on non-2xx
Editor.updateSegment(seg)       // replace a segment in state by index, re-render that row + the subtitle overlay
Editor.refresh()                // reload /editor data and re-render everything
Editor.on(eventName, fn)        // events: 'ready', 'segment-updated' (seg), 'seek' (time), 'play', 'pause', 'refreshed', 'media-changed' ({dub})
Editor.isDub()                  // true while the finished dubbed mp4 is the video source (the preview must then stay off)
Editor.syncStatus()             // call after changing Editor.state.job.status from outside (Apply flow): updates badge and read-only state
Editor.mountAudio()             // re-mounts panel + line audio; editor_audio.js calls it when it loads after the editor is ready
Editor.toast(message, kind)     // kind: 'info' | 'error' | 'ok'
Editor.formatTime(sec), Editor.parseTime(str)
```
For each line row U2 renders `<div class="line-audio" data-index="N"></div>` and, after rendering or re-rendering a row, calls
`window.EditorAudio && EditorAudio.mountLine(containerEl, segment)`. U2 also renders `<div id="audio-panel"></div>` (volume
sliders, Apply button, status) and calls `EditorAudio.mountPanel(el)` once on 'ready'.
U3 (`editor_audio.js`) creates `window.EditorAudio = { mountLine(el, seg), mountPanel(el) }`, and owns: per-line Listen / Regenerate
(with an in-page cost confirmation, never `window.confirm`) / Speed slider, the volume sliders, the in-browser dub preview (Web Audio:
schedule each line's clip at its start time against the playing video, background gain = ducked while a clip plays, otherwise
background_volume, voice gain = voice_volume; re-schedule on seek, pause, play, and when segments change), and the Apply flow
(POST apply, poll `GET /api/jobs/{id}` every 1.5 s, show stage/progress, then `Editor.refresh()`).
All user text goes into the DOM with textContent (never innerHTML with data).
