from __future__ import annotations

import json

from video_translator.config import WhisperConfig
from video_translator.doctor import WARN, check_whisper
from video_translator.models import Chunk, Segment
from video_translator.providers.asr.gemini import parse_segments
from video_translator.providers.asr.whisper_gemini import (
    WhisperGeminiASR,
    add_missed,
    parse_refinement,
)
from video_translator.providers.asr.whisper_timing import (
    TimedLine,
    WhisperTimer,
    Word,
    clean_lines,
    split_long_line,
)
from video_translator.providers.gemini_client import FakeGeminiTransport, GeminiClient
from video_translator.stages.transcribe import _load_cached, _save_cached


class FakeWhisper:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def transcribe(self, audio_path, language):
        self.calls += 1
        return [TimedLine(x.start, x.end, x.text, x.words, x.censored, x.force) for x in self.lines]


def make_asr(tmp_path, lines, responses, beeps=()):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    whisper = FakeWhisper(lines)
    fake = FakeGeminiTransport(responses)
    client = GeminiClient(model="m", requests_per_minute=1000, transport=fake, sleep=lambda s: None)
    asr = WhisperGeminiASR(client=client, whisper=WhisperConfig(), whisper_transport=whisper)
    asr.timer._beep_finder = lambda path: list(beeps)
    return asr, fake, whisper, audio


def L(start, end, text):
    return TimedLine(start, end, text)


def refinement(lines, missed=()):
    return json.dumps(
        {
            "lines": [{"id": i, "text": t} for i, t in lines],
            "missed": [{"start": a, "end": b, "text": t} for a, b, t in missed],
        },
        ensure_ascii=False,
    )


def words(*items):
    return [Word(a, b, t) for a, b, t in items]


# --- splitting and cleaning -----------------------------------------------------------------


def test_split_long_line_cuts_at_the_pause():
    ws = words(*[(i * 2.0, i * 2.0 + 1.8, f"字{i}") for i in range(10)])
    ws[4] = Word(8.0, 9.0, "停。")  # a pause plus punctuation after word 4
    line = TimedLine(0.0, 19.8, "".join(w.text for w in ws), ws)
    parts = split_long_line(line, 12.0)
    assert len(parts) >= 2
    assert all(p.end - p.start <= 12.0 for p in parts)
    assert parts[0].start == 0.0 and parts[-1].end == 19.8
    assert "".join(p.text for p in parts) == line.text


def test_split_long_line_without_words_is_unchanged():
    line = TimedLine(0.0, 40.0, "很长的一句话")
    assert split_long_line(line, 12.0) == [line]


def test_clean_lines_drops_empty_clamps_and_fixes_overlap():
    out = clean_lines(
        [L(0.0, 3.0, "你好"), L(2.5, 5.0, "世界"), L(6.0, 7.0, " "), L(9.0, 30.0, "结束")],
        12.0,
        duration=10.0,
    )
    assert [(x.start, x.end, x.text) for x in out] == [
        (0.0, 2.5, "你好"),
        (2.5, 5.0, "世界"),
        (9.0, 10.0, "结束"),
    ]


def test_timer_caches_next_to_the_chunk(tmp_path):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    fake = FakeWhisper([L(1.0, 2.0, "你好")])
    timer = WhisperTimer(WhisperConfig(), fake)
    first = timer.lines(audio, "zh", 10.0)
    second = timer.lines(audio, "zh", 10.0)
    assert fake.calls == 1 and [(x.start, x.text) for x in second] == [(1.0, "你好")]
    assert first[0].text == "你好"
    other = WhisperTimer(WhisperConfig(model="small"), fake)  # different settings: not reused
    other.lines(audio, "zh", 10.0)
    assert fake.calls == 2


# --- Whisper timing + Gemini text ---------------------------------------------------------------


def test_gemini_text_keeps_whisper_times(tmp_path):
    lines = [L(1.0, 3.0, "你号"), L(5.0, 8.0, "再见"), L(10.0, 11.0, "音乐")]
    asr, fake, _, audio = make_asr(tmp_path, lines, [refinement([(0, "你好"), (2, "")])])
    segs = asr.transcribe(audio, duration=20.0)
    # 0: corrected text; 1: Gemini did not return it, so Whisper's text stays; 2: music, dropped
    assert [(s.start, s.end, s.text_zh) for s in segs] == [(1.0, 3.0, "你好"), (5.0, 8.0, "再见")]
    assert len(fake.calls) == 1 and fake.calls[0]["audio_path"] == audio
    assert '"whisper_text": "你号"' in fake.calls[0]["prompt"]


def test_unreadable_gemini_reply_falls_back_to_whisper_text(tmp_path):
    asr, fake, _, audio = make_asr(tmp_path, [L(1.0, 3.0, "你好")], ["nope", "still nope"])
    segs = asr.transcribe(audio)
    assert [(s.start, s.text_zh) for s in segs] == [(1.0, "你好")]
    assert len(fake.calls) == 2 and any("unreadable" in n for n in asr.notes)


def test_no_speech_means_no_gemini_request(tmp_path):
    asr, fake, _, audio = make_asr(tmp_path, [], [])
    assert asr.transcribe(audio) == [] and fake.calls == []


def test_whisper_result_is_not_repeated_after_a_gemini_failure(tmp_path):
    asr, fake, whisper, audio = make_asr(
        tmp_path, [L(1.0, 3.0, "你好")], [RuntimeError("boom"), refinement([(0, "你好吗")])]
    )
    try:
        asr.transcribe(audio)
    except Exception:
        pass
    segs = asr.transcribe(audio)
    assert whisper.calls == 1 and segs[0].text_zh == "你好吗"


# --- missed lines ---------------------------------------------------------------------------


def seg(a, b, t):
    return Segment(start=a, end=b, text_zh=t)


def test_missed_line_is_clamped_inside_its_gap():
    base = [seg(0.0, 2.0, "第一句话"), seg(8.0, 10.0, "第三句话")]
    out, added = add_missed(base, [(1.0, 40.0, "漏掉的一句")], duration=60.0)
    assert added == 0  # starts inside the first line: not a gap
    out, added = add_missed(base, [(3.0, 40.0, "漏掉的一句")], duration=60.0)
    assert added == 1
    new = [s for s in out if s.text_zh == "漏掉的一句"][0]
    assert (new.start, new.end) == (3.0, 8.0)  # cannot run into the next line


def test_missed_echo_of_existing_line_is_ignored():
    base = [seg(0.0, 2.0, "第一句话"), seg(8.0, 10.0, "第三句话")]
    out, added = add_missed(base, [(4.0, 6.0, "第一句话")], duration=60.0)
    assert added == 0 and len(out) == 2


def test_parse_refinement_variants():
    texts, missed = parse_refinement('```json\n{"lines":[{"id":"1","text":" 你 好 "}]}\n```')
    assert texts == {} or texts == {1: "你 好"}
    texts, missed = parse_refinement(
        '{"lines":[{"id":1,"text":"你好"}],"missed":[{"start":"00:03","end":5,"text":"嗯"}]}'
    )
    assert texts == {1: "你好"} and missed == [(3.0, 5.0, "嗯")]


# --- cache and the old Gemini-only parser ---------------------------------------------------------


def test_cache_from_another_provider_is_not_reused(tmp_path):
    chunk = Chunk(index=0, start=0.0, end=60.0, path=tmp_path / "chunk_000.wav")
    path = tmp_path / "transcripts" / "chunk_000.json"
    _save_cached(path, chunk, [seg(1.0, 2.0, "你好")], "gemini")
    assert _load_cached(path, chunk, "gemini") is not None
    assert _load_cached(path, chunk, "whisper_gemini") is None
    path.write_text(
        path.read_text(encoding="utf-8").replace('"provider": "gemini",\n', ""), encoding="utf-8"
    )
    assert _load_cached(path, chunk, "gemini") is not None  # old files count as Gemini-only


def test_missing_end_is_not_stretched_to_a_far_away_next_line():
    raw = json.dumps(
        {"segments": [{"start": 59.0, "text": "你好"}, {"start": 103.2, "end": 105, "text": "好"}]}
    )
    first = parse_segments(raw)[0]
    assert first.end == 67.0


def test_doctor_whisper_check():
    def missing(name):
        raise ImportError(name)

    assert check_whisper("whisper_gemini", importer=missing).status == WARN
    assert check_whisper("whisper_gemini", importer=lambda n: object()).status == "OK"
    assert check_whisper("gemini", importer=missing).status == "OK"


# --- decoding without PyAV ---------------------------------------------------------------------


def _write_wav(path, seconds=0.5, rate=16000):
    import math
    import struct
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(int(seconds * rate))
            )
        )


def test_real_transport_gives_whisper_decoded_samples_not_a_path(tmp_path, monkeypatch):
    import numpy as np

    import video_translator.providers.asr.whisper_timing as wt
    from video_translator.providers.asr.whisper_timing import (
        _FasterWhisperTransport,
        decode_audio_16k,
    )

    wav = tmp_path / "chunk_000.wav"
    _write_wav(wav, 0.5, rate=44100)  # even other sample rates are converted to 16 kHz mono
    samples = decode_audio_16k(wav)
    assert samples.dtype == np.float32 and abs(len(samples) - 8000) < 200

    class FakeWord:
        def __init__(self, start, end, word):
            self.start, self.end, self.word = start, end, word

    class FakeSegment:
        start, end, text = 0.1, 0.4, "你好"
        words = [FakeWord(0.1, 0.25, "你"), FakeWord(0.25, 0.4, "好")]

    class FakeModel:
        received = None
        kwargs = None

        def transcribe(self, audio, **kwargs):
            FakeModel.received, FakeModel.kwargs = audio, kwargs
            return iter([FakeSegment()]), None

    seen = {}

    def fake_spans(audio, options):
        seen["options"] = options
        return [(0.05, 0.45)]

    monkeypatch.setattr(wt, "detect_speech_spans", fake_spans)
    cfg = WhisperConfig(pause_seconds=0.5, vad_threshold=0.2)
    transport = _FasterWhisperTransport(cfg)
    key = (cfg.model, cfg.compute_type, cfg.cpu_threads, cfg.model_dir)
    _FasterWhisperTransport._models[key] = FakeModel()
    try:
        lines = transport.transcribe(wav, "zh")
    finally:
        _FasterWhisperTransport._models.pop(key, None)
    assert isinstance(FakeModel.received, np.ndarray)  # never a file path (avoids PyAV)
    assert FakeModel.kwargs["word_timestamps"] is True
    assert seen["options"]["threshold"] == 0.2 and seen["options"]["min_silence_duration_ms"] == 500
    # the line times are the speech stretch's, the text is Whisper's
    assert [(x.start, x.end, x.text) for x in lines] == [(0.05, 0.45, "你好")]


def test_no_speech_stretch_means_whisper_is_not_run(tmp_path, monkeypatch):
    import video_translator.providers.asr.whisper_timing as wt

    wav = tmp_path / "chunk_000.wav"
    _write_wav(wav, 0.5)

    class Boom:
        def transcribe(self, *a, **k):
            raise AssertionError("Whisper must not run when there is no speech")

    monkeypatch.setattr(wt, "detect_speech_spans", lambda audio, options: [])
    cfg = WhisperConfig()
    key = (cfg.model, cfg.compute_type, cfg.cpu_threads, cfg.model_dir)
    wt._FasterWhisperTransport._models[key] = Boom()
    try:
        assert wt._FasterWhisperTransport(cfg).transcribe(wav, "zh") == []
    finally:
        wt._FasterWhisperTransport._models.pop(key, None)


def test_lines_from_spans_gives_each_word_to_its_stretch():
    from video_translator.providers.asr.whisper_timing import lines_from_spans

    spans = [(1.0, 3.0), (3.6, 5.0), (8.0, 9.0)]
    ws = words(
        (1.1, 1.8, "你好"),
        (2.0, 2.9, "吗"),
        (3.35, 3.5, "呢"),  # just before the second stretch: goes to the nearest one
        (3.7, 4.9, "再见"),
        (6.0, 6.5, "音乐"),  # in a gap, far from any stretch: dropped
    )
    out = lines_from_spans(spans, ws)
    assert [(x.start, x.end, x.text) for x in out] == [
        (1.0, 3.0, "你好吗"),
        (3.6, 5.0, "呢再见"),
        (8.0, 9.0, ""),  # a stretch without words stays: Gemini fills it in or blanks it
    ]
    assert all(x.force for x in out)


def test_detect_speech_spans_uses_the_bundled_detector(monkeypatch):
    import sys
    import types

    from video_translator.providers.asr.whisper_timing import detect_speech_spans

    mod = types.ModuleType("faster_whisper.vad")

    class VadOptions:
        def __init__(self, **kw):
            self.kw = kw

    mod.VadOptions = VadOptions
    mod.get_speech_timestamps = lambda samples, opts: [{"start": 16000, "end": 48000}]
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", mod)
    assert detect_speech_spans([0.0] * 10, {"threshold": 0.3}) == [(1.0, 3.0)]


def test_whisper_settings_changes_invalidate_the_cache(tmp_path):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    fake = FakeWhisper([TimedLine(0.0, 1.0, "你好")])
    WhisperTimer(WhisperConfig(), fake).lines(audio, "zh", 10.0)
    WhisperTimer(WhisperConfig(), fake).lines(audio, "zh", 10.0)
    assert fake.calls == 1
    WhisperTimer(WhisperConfig(pause_seconds=0.8), fake).lines(audio, "zh", 10.0)
    WhisperTimer(WhisperConfig(vad_threshold=0.2), fake).lines(audio, "zh", 10.0)
    assert fake.calls == 3


# --- beeps, pauses, gaps -----------------------------------------------------------------------


def _tone_and_noise(total=6.0, beep=(2.0, 2.8), hz=1480.0, rate=16000):
    import numpy as np

    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.05, int(total * rate)).astype("float32")  # speech-like broadband noise
    a, b = int(beep[0] * rate), int(beep[1] * rate)
    t = np.arange(b - a) / rate
    x[a:b] = 0.4 * np.sin(2 * np.pi * hz * t)
    return x


def test_detect_beeps_finds_only_the_tone():
    from video_translator.providers.asr.whisper_timing import detect_beeps

    (span,) = detect_beeps(_tone_and_noise())
    assert abs(span[0] - 2.0) <= 0.15 and abs(span[1] - 2.8) <= 0.25
    assert detect_beeps(_tone_and_noise(beep=(2.0, 2.1))) == []  # too short to be a bleep
    assert detect_beeps(_tone_and_noise(hz=300.0)) == []  # low hum is not a bleep


def test_apply_beeps_splits_a_line_and_adds_a_censored_line():
    from video_translator.providers.asr.whisper_timing import apply_beeps

    ws = words(
        (0.0, 1.0, "你"), (1.0, 2.0, "跟"), (2.0, 3.0, "他"), (4.0, 5.0, "们"), (6.0, 7.0, "好")
    )
    line = TimedLine(0.0, 7.0, "".join(w.text for w in ws), ws)
    out = apply_beeps([line], [(3.2, 3.9)])
    assert [(round(x.start, 2), round(x.end, 2), x.censored) for x in out] == [
        (0.0, 3.0, False),
        (3.15, 3.95, True),
        (4.0, 7.0, False),
    ]
    assert out[0].text == "你跟他" and out[2].text == "们好"


def test_beep_outside_any_line_gets_its_own_line_and_short_leftovers_are_absorbed():
    from video_translator.providers.asr.whisper_timing import apply_beeps

    out = apply_beeps([L(10.0, 11.0, "好")], [(2.0, 2.6)])
    assert [(x.censored, round(x.start, 2)) for x in out] == [(True, 1.95), (False, 10.0)]
    # a line starting only 0.1 s before the beep: its start becomes part of the beep line
    out = apply_beeps([TimedLine(1.9, 4.0, "它", words((3.0, 4.0, "它")))], [(2.0, 2.6)])
    assert out[0].censored and out[0].start == 1.9


def test_split_at_pauses():
    from video_translator.providers.asr.whisper_timing import split_at_pauses

    ws = words((0.0, 1.0, "你好"), (1.1, 2.0, "吗"), (3.5, 4.0, "我"), (4.0, 5.0, "很好"))
    parts = split_at_pauses(TimedLine(0.0, 5.0, "你好吗我很好", ws))
    assert [(p.start, p.end, p.text) for p in parts] == [(0.0, 2.0, "你好吗"), (3.5, 5.0, "我很好")]


def test_timer_applies_beeps_from_cache_and_settings_do_not_rerun_whisper(tmp_path):
    audio = tmp_path / "chunk_000.wav"
    audio.write_bytes(b"x")
    ws = words((0.0, 1.0, "前"), (1.0, 2.0, "面"), (5.0, 6.0, "后"), (6.0, 7.0, "面"))
    fake = FakeWhisper([TimedLine(0.0, 7.0, "前面后面", ws)])
    timer = WhisperTimer(WhisperConfig(), fake, beep_finder=lambda p: [(3.0, 3.8)])
    out = timer.lines(audio, "zh", 10.0)
    assert [x.censored for x in out] == [False, True, False]
    off = WhisperTimer(WhisperConfig(detect_beeps=False), fake, beep_finder=lambda p: [(3.0, 3.8)])
    assert [x.censored for x in off.lines(audio, "zh", 10.0)] == [False, False] and fake.calls == 1


# --- fillers ---


def test_fillers():
    from video_translator.providers.asr.cleanup import drop_fillers, is_filler

    for text in ("嗯", "嗯...", "啊啊，", "Hmm", "哈哈哈！", "呃，哦", "  "):
        assert is_filler(text), text
    for text in ("好", "你好", "嗯，我知道了", "是吗？"):
        assert not is_filler(text), text
    segs = [seg(0, 1, "嗯"), seg(2, 3, "你好")]
    assert [s.text_zh for s in drop_fillers(segs)] == ["你好"]


def test_provider_marks_censored_lines_and_drops_fillers_without_joining(tmp_path):
    lines = [
        L(0.0, 0.6, "孙总"),
        L(0.7, 2.0, "下午真不行"),
        L(3.0, 3.5, "嗯"),
    ]
    reply = refinement([(0, "孙总，"), (1, "下午真不行。"), (2, "嗯。"), (3, "他妈的")])
    asr, fake, _, audio = make_asr(tmp_path, lines, [reply], beeps=[(5.0, 5.8)])
    segs = asr.transcribe(audio, duration=20.0)
    assert [(s.start, s.end, s.text_zh) for s in segs] == [
        (0.0, 0.6, "孙总，"),
        (0.7, 2.0, "下午真不行。"),
        (4.95, 5.85, "他妈的"),
    ]
    prompt = fake.calls[0]["prompt"]
    assert '"censored": true' in prompt and '"whisper_text": "嗯"' in prompt
