# video-translator

A locally run Python tool that takes a Chinese video (30 to 60 minutes is the target) and
produces a Vietnamese version: a Vietnamese dubbed voice plus a Vietnamese `.srt` subtitle
file. It uses **Whisper** (runs on your computer) to hear the Chinese, **9Router** (a free local
proxy to free AI models) to translate, with **Gemini** (free tier) as an automatic fallback, and
**VieNeu-TTS** (Vietnamese voice: free, open source, runs on your own computer, no account and
no limits). Nothing here needs a paid plan. It runs on Windows, CPU only, and is controlled from
a small local web page or from the command line.

> **Tiếng Việt:** hướng dẫn cài đặt từng bước bằng tiếng Việt nằm ngay bên dưới phần tiếng Anh
> ([nhảy tới phần tiếng Việt](#hướng-dẫn-cài-đặt-từng-bước-windows-tiếng-việt)).

## Step-by-step install and setup (Windows, English)

This guide assumes a Windows 10 or 11 PC where **nothing is installed yet**, and that the project
files are in `C:\video-translator`. You need an internet connection for the downloads (a few GB
in total, most of it one-time model downloads), about 10 GB of free disk space, and 8 GB of RAM is
comfortable (4 GB is the minimum). Everything is free; nothing needs a paid plan.

Type the commands in **PowerShell** (Start menu, type `PowerShell`). After installing a program,
**close PowerShell and open a new one** so it finds the new program.

### Step 1: Install Python

```powershell
winget install Python.Python.3.12
```

Close and reopen PowerShell, then check: `python --version` should print 3.10 or newer (3.10 to
3.12 is the safest). If `winget` is not available, download Python from https://www.python.org/downloads/
and tick **Add python.exe to PATH** in the installer.

### Step 2: Install ffmpeg (audio and video tool)

```powershell
winget install Gyan.FFmpeg
```

Reopen PowerShell, then check: `ffmpeg -version` prints a version line.

### Step 3: Install Node.js (needed only to run 9Router)

```powershell
winget install OpenJS.NodeJS.LTS
```

Reopen PowerShell, then check: `node --version` and `npm --version` print versions.

### Step 4: Create the Python environment and install the tool

```powershell
cd C:\video-translator
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

You should see `(.venv)` at the start of the prompt. This takes a few minutes (it also installs
Whisper, the speech-timing model package). If PowerShell says running scripts is disabled, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try the activate line again.

### Step 5: Install the Vietnamese voice (VieNeu-TTS)

With `(.venv)` showing:

```powershell
pip install vieneu
```

The voice model downloads from the internet the first time it speaks (a few minutes, once).

### Step 6: Install and set up 9Router (the free translation models)

Open a **second** PowerShell window (9Router keeps running in it) and run:

```powershell
npm install -g 9router
9router
```

Then in your browser:

1. Open `http://localhost:20128`.
2. Go to **Providers** and connect the free ones you want (for example **Kiro AI** and
   **OpenCode Free**). Follow the sign-in steps on the page.
3. Go to **Combos**, create a combo named exactly **`free-combo`** and add your connected free
   models to it, in the order you prefer.
4. If your 9Router has key checking on (translation later fails with `401 Invalid API key`), look
   for **API Keys** or the **Endpoint** page in the dashboard and create or copy a key. You will
   put it into `.env` in Step 7.

Leave this window open whenever you translate. The tool tries your free models first and only
uses Gemini (Step 7) if every free model fails or runs out of its monthly limit.

### Step 7: Put your keys in the `.env` file

In the first PowerShell window (the one with `(.venv)`):

```powershell
copy .env.example .env
notepad .env
```

Fill in and save (keep the file name `.env`, not `.env.txt`):

```
GEMINI_API_KEY=your-key-here
NINEROUTER_API_KEY=
```

- **`GEMINI_API_KEY`** (free): create it at https://aistudio.google.com/apikey. It is the safety
  net when the free 9Router models fail, so please set it.
- **`NINEROUTER_API_KEY`**: leave empty unless 9Router asked for a key in Step 6, then paste it.

Never share `.env`. Keys are never shown on screen or saved in job files.

### Step 8 (optional): Your own settings in `config\local.yaml`

Do not edit `config\default.yaml`. Create `config\local.yaml` (Notepad, save as that exact name)
with only what you want to change. The tool re-reads it every time a job starts or resumes.
Example:

```yaml
vieneu:
  voice: "Mai Anh"            # list voices with: video-translator voices (or pick on the upload page)
whisper:
  model: small                # faster than the default "medium", a little less exact
ninerouter:
  models: [free-combo]        # your combo name(s) from the 9Router dashboard
separation:
  enabled: false              # true = separate voice and music for every video (Step 9)
```

### Step 9 (optional): Voice separation for loud music (Demucs)

Only needed for videos where music drowns the speech. With `(.venv)` showing:

```powershell
pip install demucs
```

Then tick **Separate the voice from the music first** on the upload page for that video. It takes
about as long as the video itself on a CPU, and downloads a model (about 80 MB) the first time.
See "Voice separation" below for what it changes in the editor.

### Step 10: Check that everything works

```powershell
video-translator doctor
```

Every line should say `[OK]`. A `[WARN]` is a thing you can fix later (for example a missing
key or 9Router not running yet); a `[FAIL]` must be fixed first. The doctor also checks that
9Router answers and knows your `free-combo`. If PowerShell says `video-translator` is not
recognized, activate the environment (`.venv\Scripts\Activate.ps1`) or use
`python -m video_translator doctor`.

### Step 11: Translate your first video

```powershell
video-translator serve
```

Open http://127.0.0.1:8000, upload a video, and wait. **Try a clip of one or two minutes first**
to make sure everything works before you start a long video. When the job says "awaiting review",
read and fix the Vietnamese lines, then press **Approve**. When it is done, download the `.mp4`
and the `.srt`, or open the **editor** to adjust lines, timing, volumes and the blur box.

The first run downloads the Whisper model (about 0.8 GB) and the voice model; this is normal and
happens once.

### Every day: how to start

1. **Window 1:** `9router` (leave it open).
2. **Window 2:**

   ```powershell
   cd C:\video-translator
   .venv\Scripts\Activate.ps1
   video-translator serve
   ```

3. Open http://127.0.0.1:8000. To stop, press `Ctrl+C` in each window.

After you copy new project files into `C:\video-translator`, stop and restart `serve` and press
**Ctrl+F5** in the browser. If a job failed, fix the cause and press **Try again (resume)**; the
steps that already finished are not repeated.

If something goes wrong, see "Troubleshooting" further down this page.

---

## Hướng dẫn cài đặt từng bước (Windows, tiếng Việt)

Hướng dẫn này dành cho máy Windows 10 hoặc 11 **chưa cài gì**, và giả định các file của dự án nằm
trong `C:\video-translator`. Bạn cần có internet để tải (tổng cộng vài GB, phần lớn là tải
model một lần), còn trống khoảng 10 GB ổ đĩa, RAM 8 GB là thoải mái (tối thiểu 4 GB). Mọi thứ đều
miễn phí, không cần gói trả phí nào.

Gõ lệnh trong **PowerShell** (menu Start, gõ `PowerShell`). Sau khi cài một chương trình, hãy
**đóng PowerShell và mở cửa sổ mới** để máy nhận ra chương trình đó.

### Bước 1: Cài Python

```powershell
winget install Python.Python.3.12
```

Đóng và mở lại PowerShell, rồi kiểm tra: `python --version` phải hiện từ 3.10 trở lên (3.10 đến
3.12 là an toàn nhất). Nếu không có `winget`, tải Python tại https://www.python.org/downloads/ và
tick ô **Add python.exe to PATH** khi cài.

### Bước 2: Cài ffmpeg (công cụ xử lý âm thanh và video)

```powershell
winget install Gyan.FFmpeg
```

Mở lại PowerShell, rồi kiểm tra: `ffmpeg -version` hiện một dòng phiên bản.

### Bước 3: Cài Node.js (chỉ để chạy 9Router)

```powershell
winget install OpenJS.NodeJS.LTS
```

Mở lại PowerShell, rồi kiểm tra: `node --version` và `npm --version` hiện phiên bản.

### Bước 4: Tạo môi trường Python và cài công cụ

```powershell
cd C:\video-translator
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Đầu dòng lệnh phải có `(.venv)`. Bước này mất vài phút (nó cũng cài Whisper, gói nhận dạng giọng
nói và canh thời gian). Nếu PowerShell báo bị chặn chạy script, chạy một lần
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` rồi thử lại dòng activate.

### Bước 5: Cài giọng đọc tiếng Việt (VieNeu-TTS)

Khi đang thấy `(.venv)`:

```powershell
pip install vieneu
```

Model giọng đọc được tải từ internet ở lần đọc đầu tiên (vài phút, chỉ một lần).

### Bước 6: Cài và thiết lập 9Router (các model dịch miễn phí)

Mở cửa sổ PowerShell **thứ hai** (9Router chạy liên tục trong đó) và chạy:

```powershell
npm install -g 9router
9router
```

Sau đó trong trình duyệt:

1. Mở `http://localhost:20128`.
2. Vào **Providers** và kết nối các nhà cung cấp miễn phí bạn muốn (ví dụ **Kiro AI** và
   **OpenCode Free**). Làm theo các bước đăng nhập trên trang.
3. Vào **Combos**, tạo một combo tên đúng là **`free-combo`** và thêm các model miễn phí đã kết
   nối vào, theo thứ tự bạn muốn.
4. Nếu 9Router của bạn bật kiểm tra khóa (khi dịch bị lỗi `401 Invalid API key`), hãy tìm mục
   **API Keys** hoặc trang **Endpoint** trong dashboard rồi tạo hoặc sao chép một khóa. Bạn sẽ dán
   khóa đó vào `.env` ở Bước 7.

Hãy để cửa sổ này mở mỗi khi dịch. Công cụ thử các model miễn phí trước, và chỉ dùng Gemini (Bước
7) khi mọi model miễn phí đều lỗi hoặc hết hạn mức hàng tháng.

### Bước 7: Điền khóa vào file `.env`

Trong cửa sổ PowerShell thứ nhất (cửa sổ có `(.venv)`):

```powershell
copy .env.example .env
notepad .env
```

Điền và lưu (giữ tên file là `.env`, không phải `.env.txt`):

```
GEMINI_API_KEY=khoa-cua-ban
NINEROUTER_API_KEY=
```

- **`GEMINI_API_KEY`** (miễn phí): tạo tại https://aistudio.google.com/apikey. Đây là phương án dự
  phòng khi các model miễn phí của 9Router lỗi, nên hãy điền.
- **`NINEROUTER_API_KEY`**: để trống, trừ khi 9Router đã yêu cầu khóa ở Bước 6, khi đó hãy dán vào.

Không chia sẻ file `.env`. Khóa không bao giờ hiện trên màn hình hay lưu trong file công việc.

### Bước 8 (tùy chọn): Cài đặt riêng trong `config\local.yaml`

Đừng sửa `config\default.yaml`. Hãy tạo `config\local.yaml` (Notepad, lưu đúng tên đó) chỉ chứa
những gì bạn muốn đổi. Công cụ đọc lại file này mỗi khi một công việc bắt đầu hoặc chạy tiếp. Ví dụ:

```yaml
vieneu:
  voice: "Mai Anh"            # xem danh sách giọng: video-translator voices (hoặc chọn trên trang tải lên)
whisper:
  model: small                # nhanh hơn "medium" mặc định, kém chính xác hơn một chút
ninerouter:
  models: [free-combo]        # tên combo trong dashboard 9Router
separation:
  enabled: false              # true = tách giọng khỏi nhạc cho mọi video (Bước 9)
```

Có thể chọn giọng ngay trên trang tải lên: mở mục **Vietnamese voice** phía trên nút tải lên (lần
đầu mở sẽ tải danh sách giọng, có thể mất khoảng một phút), chọn một giọng rồi bấm **Play sample**
để nghe câu mẫu, hoặc gõ câu của bạn vào ô trước khi nghe. Giọng được chọn chỉ dùng **cho video
này**. Tick **Make this my default voice** để lưu luôn làm giọng mặc định (`vieneu.voice` trong
`config/local.yaml`). Không hỗ trợ nhân bản giọng từ file ghi âm.

### Bước 9 (tùy chọn): Tách giọng khỏi nhạc khi nhạc quá to (Demucs)

Chỉ cần cho video mà nhạc lấn át tiếng nói. Khi đang thấy `(.venv)`:

```powershell
pip install demucs
```

Sau đó tick ô **Separate the voice from the music first** trên trang tải lên cho video đó. Việc
tách mất khoảng bằng độ dài video trên CPU, và tải một model (khoảng 80 MB) ở lần đầu. Xem mục
"Voice separation" bên dưới để biết nó thay đổi gì trong trình chỉnh sửa.

### Bước 10: Kiểm tra mọi thứ đã chạy được

```powershell
video-translator doctor
```

Mỗi dòng nên là `[OK]`. `[WARN]` là thứ có thể sửa sau (ví dụ thiếu khóa hoặc 9Router chưa chạy);
`[FAIL]` phải sửa trước. Lệnh này cũng kiểm tra 9Router có trả lời và có biết `free-combo` không.
Nếu PowerShell báo không nhận ra `video-translator`, hãy kích hoạt môi trường
(`.venv\Scripts\Activate.ps1`) hoặc dùng `python -m video_translator doctor`.

### Bước 11: Dịch video đầu tiên

```powershell
video-translator serve
```

Mở http://127.0.0.1:8000, tải video lên và chờ. **Hãy thử một đoạn dài một hai phút trước** để chắc
mọi thứ chạy ổn rồi mới làm video dài. Khi công việc báo "awaiting review", hãy đọc và sửa các câu
tiếng Việt rồi bấm **Approve**. Khi xong, tải file `.mp4` và `.srt`, hoặc mở **editor** để chỉnh
câu, thời gian, âm lượng và khung làm mờ.

Lần chạy đầu sẽ tải model Whisper (khoảng 0,8 GB) và model giọng đọc; việc này bình thường và chỉ
xảy ra một lần.

### Mỗi ngày: cách khởi động

1. **Cửa sổ 1:** `9router` (để mở).
2. **Cửa sổ 2:**

   ```powershell
   cd C:\video-translator
   .venv\Scripts\Activate.ps1
   video-translator serve
   ```

3. Mở http://127.0.0.1:8000. Để dừng, bấm `Ctrl+C` trong từng cửa sổ.

Sau khi chép file dự án mới vào `C:\video-translator`, hãy dừng rồi chạy lại `serve` và bấm
**Ctrl+F5** trong trình duyệt. Nếu một công việc bị lỗi, hãy sửa nguyên nhân rồi bấm **Try again
(resume)**; các bước đã xong sẽ không phải làm lại.

Nếu có sự cố, xem mục "Troubleshooting" (xử lý lỗi, bằng tiếng Anh) ở phía dưới trang này.

---

## How it works

1. **Extract audio**: ffmpeg pulls the audio out of the video and cuts it into overlapping
   chunks (default 5 minutes).
2. **Transcribe**: a speech detector listens to the audio and finds each *stretch of speech*;
   every pause of 0.2 seconds or more, and every sentence end (。！？), starts a new line, and the line's start and end are the
   stretch's own start and end. A local speech model (faster-whisper) supplies both the timing
   and the Chinese words, so subtitles line up with the speech. Extra care: a **beep tone** (a
   bleeped swear word) is detected in the audio and gets its own line; if Whisper caught part of
   the word it is kept, otherwise the line is marked （哔） and the translator guesses the word
   from the context (check these in the review step); filler sounds ("hmm", 嗯, 啊, laughter)
   and Whisper's known inventions (for example 请不吝点赞) never become lines.
3. **Translate**: **9Router** translates the segments into spoken Vietnamese, in batches. It
   tries your free models in the order you set; only when every free model has failed or is out
   of quota does **Gemini** take over, automatically, for that batch.
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

## Setup details (reference)

The step-by-step guide above is all you need; this section keeps the details.

ffmpeg must be installed separately and available on your `PATH` (Windows:
`winget install Gyan.FFmpeg`, then reopen the terminal).

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1     # Windows PowerShell; on Linux/macOS: source .venv/bin/activate
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

Three more settings control the line timing: `whisper.pause_seconds` (default 0.2: a pause this
long starts a new line), `whisper.split_sentences` (default true: also start a new line after
each sentence end, using Whisper's word times) and `whisper.vad_threshold` (default 0.3: lower hears quieter speech,
higher ignores more background noise).

Whisper does the listening by itself. Compared with letting Gemini also listen, expect more
mistakes on mumbled or noisy speech, and quiet speech under loud music may be missed. To go back
to the old way (Gemini corrects Whisper's words and adds missed lines), set
`providers: {asr: whisper_gemini}`.

### Translation: 9Router with Gemini as the fallback

9Router is a free program that runs on your computer and gives one address for many free AI
models. Install [Node.js](https://nodejs.org), then in a second terminal:

```bash
npm install -g 9router
9router
```

1. Open the dashboard at `http://localhost:20128`.
2. Under **Providers**, connect the free ones you want (for example Kiro and OpenCode Free).
3. Under **Combos**, create a combo named `free-combo` that lists those free models in the
   order you prefer. (You can also type model names directly in `ninerouter.models`.)

Keep 9Router running while a job translates. The settings, in `config/local.yaml` if you want to
change them:

```yaml
ninerouter:
  base_url: http://localhost:20128/v1
  models: [free-combo]        # tried in order; the next one is tried when one fails
  requests_per_minute: 20
  batch_size: 10
```

If 9Router answers **401 "Invalid API key"**, your 9Router has key checking on. Copy a key from
its dashboard (`http://localhost:20128`, look for **API Keys** or the **Endpoint** page and use
its create/copy button), put this line in the `.env` file **in the folder you start the tool
from** (the same place as `GEMINI_API_KEY`), and restart `serve`:

```
NINEROUTER_API_KEY=the-key-you-copied
```

The tool only reads `.env` from the folder you run it in, so start `serve` from
`C:\video-translator`. If you would rather not use a key, start 9Router with its
`REQUIRE_API_KEY` setting off (the default).
A model that fails is rested for a while (15 minutes for a quota error, a day if it is refused
or does not exist, 2 minutes for a bad answer) and the next model is used. When every model is
resting or failing, Gemini translates that batch (it needs `GEMINI_API_KEY`, free tier). The
job message shows how many lines each model translated. To use Gemini only, set
`providers: {translation: gemini}`.

Free providers can see the transcript text, and they have monthly limits; a long video can use
up a free model, which is what the fallback is for. The Vietnamese may read less naturally than
Gemini's, so use the review step. The `python -m video_translator doctor` check tells you whether
9Router answers.

### How a line is fitted to its time (three steps)

A Vietnamese line is usually longer than the Chinese one, so each line is fitted in three steps:

1. **Write it to fit.** The translator is told each line's time and a maximum length
   (`fit.chars_per_second`, default 15 characters per second) and may shorten or rephrase freely,
   keeping the meaning and tone.
2. **Re-check and rewrite.** Lines that would still need more than a 1.2x speed-up are sent back
   to the translator to be shortened (up to `fit.rewrite_rounds`, default 2; 0 turns it off). This
   happens before the review step, so you review the final text. The job message tells how many
   were shortened and how many are still long.
3. **Light stretch.** When the voice is made, a clip may run up to 0.3 seconds past its line (but
   never into the next line); if it is longer it is sped up to 1.2x, rushed up to 1.35x if needed,
   and a clip shorter than its line is slowed down to at most 0.85x to fill it. A line that still
   does not fit is marked "too long" in the editor and may run into the silence after it.

### Voice separation (optional, per video)

When music or noise is loud, Whisper can miss quiet lines and the timing gets worse. You can
split the voice from the music first with **Demucs** (free, runs on your computer). The voice
alone is then used for the **timing and the transcription only**; the final mix still uses the
original sound (music and effects stay).

1. With the virtual environment activated, run `pip install demucs`. It also installs PyTorch (a
   download of a few hundred MB). The first separation downloads the model (about 80 MB), so
   internet is needed once.
2. On the upload form, tick **Separate the voice from the music first** for that video. From the
   command line use `video-translator run video.mp4 --separate-voice`. To make it the default for
   every video, put `separation: {enabled: true}` in `config/local.yaml`.
3. Expect the separation to take about as long as the video is long on a CPU (an hour of video
   takes roughly an hour, plus the usual Whisper time). Every finished two-minute piece is saved,
   so a stopped job continues where it stopped. Keep it off for videos with clear speech.

With separation on, the final mix is built from the separated parts: the **background music**
replaces the original sound, so the Chinese voice is gone by default. In the editor the timeline
shows a separate **Chinese voice** track and a **Background music** track (each with its own mute
button and waveform), and the volume panel has a slider for each: Vietnamese voice, background
music, and **Chinese voice** (it starts at 0%; raise it to keep the Chinese voice under the
dub). Music and Chinese voice are both lowered while the Vietnamese voice speaks. Press "Apply and
re-render" to hear the exact result; the in-browser preview plays the two parts in step with the
video.

The beep detector still listens to the original sound, because a separated voice can lose the
bleep tone. `python -m video_translator doctor` shows whether Demucs is installed.

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

### Every time you start (Windows PowerShell)

Open PowerShell and run these two lines first, before any `video-translator` command
(`serve`, `run`, `resume`, `doctor`, ...):

```powershell
cd C:\video-translator
.venv\Scripts\Activate.ps1
```

You should see `(.venv)` at the start of the prompt. Do this in every new PowerShell window;
without it `video-translator` is "not recognized". Then, for example:

```powershell
video-translator serve
```

If PowerShell says running scripts is disabled, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again (in Command Prompt use
`.venv\Scripts\activate.bat` instead).

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

**Or pick it on the upload page.** Open **Vietnamese voice** above the upload button. The first
time it opens it loads the voice list (this loads the voice model, so it can take a minute). Choose
a voice, press **Play sample** to hear it say the sample sentence, or type your own sentence in
the box first. When you like one, upload the video: the voice is used **for this video only**.
Tick **Make this my default voice** to also save it as `vieneu.voice` in `config/local.yaml`. Each
sample is kept, so hearing the same voice and sentence again is instant. In server mode the voices
cannot be listed: type a voice name instead. Cloning a voice from your own recording is not
supported.

### 3. Run it: web page

Activate the environment first (`.venv\Scripts\Activate.ps1`, see "Every time you start"), then:

```bash
video-translator serve
```

Open http://127.0.0.1:8000 (only reachable from this computer). Upload a video, watch the
progress, and when the job says "awaiting review" fix the Vietnamese lines in the page and
press Approve. When it is done, download the `.mp4` and the `.srt`. A paused job (for example the
Gemini daily quota) has a Resume button.

### 4. Run it: command line

Activate the environment first (`.venv\Scripts\Activate.ps1`), then:

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
the Vietnamese subtitles on top (about three quarters of the page; the lines are on the right, about
a quarter), a **Play** button, `Space` and a click on the picture to play or pause (the browser's own
video bar is not shown), a timeline of all lines and one row per line:

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
  editor. A box appears over the video: click it to see its outline and handles (click elsewhere
  to hide them again), then drag it to move it or drag its edges or corners to resize it (or
  type the position and size in pixels of the original video). Set the blur strength,
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
  (the original audio with its waveform; the final mix keeps the music, so it also
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
- **9Router free models** have monthly limits and the tool waits to stay under
  `ninerouter.requests_per_minute`. When they run out, Gemini translates instead.
- **Gemini** free tier (the fallback): strict per-minute and per-day request limits. If it hits
  the daily quota too, the job pauses (status "paused"); run `resume` the next day and it
  continues from the last finished batch.

### What version 1 does not do

- **No music/voice separation in the final mix.** (Separation only helps the listening, see
  above.) The original audio is ducked (lowered to
  `audio.ducked_volume`, default 25%) under the Vietnamese voice, so the Chinese voice is
  still faintly audible, and so is the music and effects at the lowered level. (Bleep tones of
  censored words are the exception: they are silenced completely.)
- **Burned-in subtitles only with the blur box.** Otherwise only a separate `.srt` is written
  (`subtitles.burn_in` is ignored with a warning); turn the blur box on in the editor to burn
  the Vietnamese subtitles into the video.
- One speaker/voice for the whole video; no per-speaker voices or voice cloning.
- Translation uses cloud models (free ones through 9Router, then Gemini); the voice and the
  listening are local (CPU), with no GPU tuning.

### Troubleshooting transcription

- **"The faster-whisper package is not installed"**: activate the virtual environment and run
  `pip install faster-whisper`.
- **"Could not load the Whisper model"**: the first run downloads it from Hugging Face; connect
  to the internet once and try again (or choose a smaller `whisper.model`).
- **Old videos still have shifted subtitles**: transcripts made before this change are not
  reused; upload the video again to get a new job.
- **A bleeped word is missing or wrong**: Whisper cannot hear under the beep, so the translator
  guesses the word from the context (the line was marked （哔）). Fix it in the review step or
  the editor. To turn the beep handling off, use `whisper: {detect_beeps: false}`.
- **A real line is still missing** (quiet speech under loud music): lower
  `whisper: {vad_threshold: 0.2}`; or use `providers: {asr: whisper_gemini}` so Gemini adds lines
  it hears in a gap; or tick **Separate the voice from the music first** for that video.
- **I changed a setting but the error still shows the old one**: personal settings go in
  `C:\video-translator\config\local.yaml` (create it if it is missing; it sits next to
  `default.yaml`). The tool re-reads it whenever a job starts or resumes, so press "Try again
  (resume)"; restart `serve` only if that does not help. Example for a combo named `my-combo`:

  ```yaml
  ninerouter:
    models: [my-combo]
  ```
- **"9Router is not reachable: timed out"**: 9Router is not running, or the free model is slow
  (a combo can try several models before it answers). Check that the `9router` window is open
  and `python -m video_translator doctor` says reachable. The wait per request is
  `ninerouter.timeout_seconds` (default 300); smaller batches answer faster (the default is 10 lines)
  (`ninerouter: {batch_size: 5}`).
- **9Router answers 404 "No active credentials for provider: openai"**: 9Router does not know
  the model name `free-combo`, so it treated it as an OpenAI model. In the dashboard, open
  **Combos**, create a combo named exactly `free-combo` and add your connected free models to it
  (or skip the combo and put a model name that 9Router lists in `ninerouter.models`, for example
  `[kr/claude-sonnet-4.5]`). Run `python -m video_translator doctor`: it lists the names 9Router
  knows. Then press "Try again (resume)".
- **"GEMINI_API_KEY is not set"**: only the fallback needs it. Get a free key at
  https://aistudio.google.com/apikey, add `GEMINI_API_KEY=the-key` to `.env` in
  `C:\video-translator` (copy `.env.example` to `.env` if you have none), and restart `serve`.
- **"No free model in 9Router could translate this batch"**: 9Router is not running, or no free
  model is connected. Start `9router`, check the dashboard, and run the doctor command.
- **Lines are cut too often or too rarely**: change `whisper: {pause_seconds: 0.6}` (longer
  pause needed) or `0.3` (shorter pause is enough).
- **The beep is still audible in the dub**: the original bleep tone is silenced in the final
  audio (`audio: {remove_beeps: true}`, the default); set it to `false` to keep it.

### Troubleshooting the editor page

- **The page looks old after an update** (for example the old layout): stop `serve` with
  Ctrl+C, start it again, and reload the page with Ctrl+F5. Since this version the page links
  carry a file version, so a normal reload is enough from now on.

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

- [x] Separate vocals from music/effects (Demucs), per video: done (see "Voice separation")
- [ ] Local transcription/translation models and GPU use
- [ ] More than one speaker/voice
- [ ] Other translation/TTS providers
