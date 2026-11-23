"""VieNeuTTS with fakes only: no real ``vieneu`` package and no network are ever used."""

from __future__ import annotations

import http.server
import sys
import threading
import types

import pytest

from video_translator.config import AppConfig
from video_translator.providers import registry
from video_translator.providers.tts import vieneu
from video_translator.providers.tts.base import TTSError, TTSSetupError, TTSVoiceError
from video_translator.providers.tts.vieneu import VieNeuTTS

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "


class FakeTransport:
    def __init__(self, presets=None):
        self.calls = []
        self.presets = (
            [("Minh Quân", "id-1"), ("Mai Anh", "id-2"), ("Trúc Ly", "id-3")]
            if presets is None
            else presets
        )

    def synthesize(self, text, voice, out_path):
        self.calls.append((text, voice))
        out_path.write_bytes(WAV)

    def list_voices(self):
        return self.presets


def make(**kw):
    cfg = AppConfig()
    return VieNeuTTS(config=cfg, transport=kw.pop("transport", FakeTransport()), **kw)


def test_registered_and_defaults():
    assert registry.get("tts", "vieneu") is VieNeuTTS
    cfg = AppConfig()
    assert cfg.providers.tts == "vieneu"
    assert cfg.vieneu.mode == "library" and cfg.vieneu.voice == "Trúc Ly"
    assert cfg.vieneu.precision == "fp32" and cfg.vieneu.timeout_seconds == 300


def test_synthesize_writes_file_and_uses_config_voice(tmp_path):
    tts = make()
    out = tts.synthesize("Xin chào", tmp_path / "sub" / "a.wav")
    assert out == tmp_path / "sub" / "a.wav" and out.read_bytes() == WAV
    assert tts._transport.calls == [("Xin chào", "id-3")]  # the engine gets the id, not the label
    assert not list((tmp_path / "sub").glob("*.part*"))


def test_voice_parameter_overrides_config_and_matching_is_case_insensitive(tmp_path):
    tts = make()
    tts.synthesize("a", tmp_path / "a.wav", voice="mai anh")
    assert tts._transport.calls == [("a", "id-2")]


def test_a_display_label_is_turned_into_the_voice_id_before_speaking(tmp_path):
    transport = FakeTransport(
        presets=[
            ("⭐ Ngọc Huyền — Nữ · Bắc · Giọng đọc tự nhiên", "Ngọc Huyền"),
            ("Mai Anh", "Mai Anh"),
        ]
    )
    tts = make(transport=transport)
    tts.synthesize("a", tmp_path / "a.wav", voice="⭐ Ngọc Huyền — Nữ · Bắc · Giọng đọc tự nhiên")
    tts.synthesize("b", tmp_path / "b.wav", voice="ngọc huyền")
    assert transport.calls == [("a", "Ngọc Huyền"), ("b", "Ngọc Huyền")]
    assert tts.list_voice_choices()[0] == (
        "⭐ Ngọc Huyền — Nữ · Bắc · Giọng đọc tự nhiên",
        "Ngọc Huyền",
    )


def test_empty_text_is_an_error(tmp_path):
    with pytest.raises(TTSError, match="no text"):
        make().synthesize("   ", tmp_path / "a.wav")


def test_unknown_voice_lists_valid_presets(tmp_path):
    with pytest.raises(TTSVoiceError, match="id-1, id-2"):
        make().synthesize("a", tmp_path / "a.wav", voice="Nobody")


def test_unlistable_voices_skip_the_check(tmp_path):
    tts = make(transport=FakeTransport(presets=[]))
    tts.synthesize("a", tmp_path / "a.wav", voice="Anything")
    assert tts.list_voices() == []


def test_list_voices_returns_labels():
    assert make().list_voices() == ["Minh Quân", "Mai Anh", "Trúc Ly"]


def test_empty_output_file_is_an_error(tmp_path):
    class Silent(FakeTransport):
        def synthesize(self, text, voice, out_path):
            out_path.write_bytes(b"")

    with pytest.raises(TTSError, match="no audio"):
        make(transport=Silent()).synthesize("a", tmp_path / "a.wav")
    assert not list(tmp_path.glob("*"))


# ----- library mode with a fake `vieneu` module ----------------------------------------------


class FakeVieneuModule(types.ModuleType):
    def __init__(self, fail_init=None):
        super().__init__("vieneu")
        self.instances = []
        self.fail_init = fail_init
        module = self

        class Vieneu:
            def __init__(self, precision="fp32"):
                if module.fail_init:
                    raise module.fail_init
                self.precision = precision
                self.calls = []
                self.active = 0
                self.max_active = 0
                module.instances.append(self)

            def list_preset_voices(self):
                return [("Minh Quân", "a"), ("Mai Anh", "b"), ("Trúc Ly", "c")]

            def infer(self, text, voice=None):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append((text, voice))
                self.active -= 1
                return [0.0, 0.1]

            def save(self, audio, path):
                with open(path, "wb") as fh:
                    fh.write(WAV)

        self.Vieneu = Vieneu


@pytest.fixture
def lib(monkeypatch):
    vieneu.reset_shared_model()
    module = FakeVieneuModule()
    monkeypatch.setitem(sys.modules, "vieneu", module)
    yield module
    vieneu.reset_shared_model()


def library_tts():
    return VieNeuTTS(config=AppConfig())  # default: real library transport


def test_library_mode_one_shared_instance_and_serialized_calls(lib, tmp_path):
    a, b = library_tts(), library_tts()
    errors = []

    def work(tts, n):
        try:
            tts.synthesize(f"câu {n}", tmp_path / f"{n}.wav", voice="Mai Anh")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=((a, b)[n % 2], n)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(lib.instances) == 1
    assert lib.instances[0].max_active == 1
    assert len(lib.instances[0].calls) == 8
    assert (tmp_path / "3.wav").read_bytes() == WAV
    assert a.list_voices() == ["Minh Quân", "Mai Anh", "Trúc Ly"]


def test_library_precision_is_passed_to_the_model(lib, tmp_path):
    cfg = AppConfig()
    cfg.vieneu.precision = "int8"
    VieNeuTTS(config=cfg).synthesize("a", tmp_path / "a.wav")
    assert lib.instances[0].precision == "int8"


def test_library_missing_package_gives_install_instructions(monkeypatch, tmp_path):
    vieneu.reset_shared_model()
    monkeypatch.setitem(sys.modules, "vieneu", None)  # makes `import vieneu` fail
    with pytest.raises(TTSSetupError, match="pip install vieneu"):
        library_tts().synthesize("a", tmp_path / "a.wav")


def test_library_model_download_failure_mentions_internet(monkeypatch, tmp_path):
    vieneu.reset_shared_model()
    monkeypatch.setitem(sys.modules, "vieneu", FakeVieneuModule(fail_init=OSError("offline")))
    with pytest.raises(TTSSetupError, match="internet") as info:
        library_tts().synthesize("a", tmp_path / "a.wav")
    assert "offline" in str(info.value)
    vieneu.reset_shared_model()


def test_importing_the_provider_does_not_import_vieneu():
    import subprocess

    code = (
        "import sys; import video_translator.providers.tts.vieneu\n"
        "assert 'vieneu' not in sys.modules"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# ----- server mode against a tiny local HTTP server ------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):
        import json

        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append((self.path, body))
        payload = WAV if self.status == 200 else b'{"error": "bad voice"}'
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.seen = []
    _Handler.status = 200
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def server_tts(url):
    cfg = AppConfig()
    cfg.vieneu.mode = "server"
    cfg.vieneu.server_url = url
    cfg.vieneu.timeout_seconds = 5
    return VieNeuTTS(config=cfg)


def test_server_mode_posts_openai_style_request(server, tmp_path):
    out = server_tts(server).synthesize("Xin chào", tmp_path / "a.wav", voice="Mai Anh")
    assert out.read_bytes() == WAV
    assert _Handler.seen == [
        (
            "/v1/audio/speech",
            {
                "model": "vieneu-v3-turbo",
                "input": "Xin chào",
                "voice": "Mai Anh",
                "response_format": "wav",
            },
        )
    ]
    assert server_tts(server).list_voices() == []


def test_server_http_error_is_reported(server, tmp_path):
    _Handler.status = 400
    with pytest.raises(TTSError, match="HTTP 400.*bad voice"):
        server_tts(server).synthesize("a", tmp_path / "a.wav")


def test_server_not_running_explains_how_to_start_it(tmp_path):
    with pytest.raises(TTSSetupError, match="apps.openai_speech"):
        server_tts("http://127.0.0.1:9").synthesize("a", tmp_path / "a.wav")
