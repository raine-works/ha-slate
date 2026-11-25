import array
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from slate_agent import protocol
from slate_agent.security import Pairing, ensure_certificate
from slate_agent.settings import Settings, SettingsStore
from slate_agent.wakeword import (
    MAX_ACTIVE, TYPE_MICRO, TYPE_OWW, WakeWordEngine, WakeWordError, WakeWordModel, WakeWordStore,
)

from .helpers import FAKE_TFLITE, micro_config


# --- protocol / security -------------------------------------------------------

def test_hmac_proof_roundtrip():
    nonce = "00112233445566778899aabbccddeeff"
    p = protocol.proof("secret", nonce)
    assert protocol.verify_proof("secret", nonce, p)
    assert not protocol.verify_proof("other", nonce, p)


def test_pairing_window(tmp_path):
    pairing = Pairing(tmp_path / "token", window_s=60)
    token, err = pairing.try_pair(None)
    assert token and err is None and pairing.paired
    assert pairing.try_pair(None) == (None, "already_paired")
    pairing.unpair()
    assert not pairing.paired


def test_pairing_window_closed(tmp_path):
    pairing = Pairing(tmp_path / "token", window_s=-1)
    assert pairing.try_pair(None) == (None, "pairing_window_closed")


def test_pairing_code_and_lockout(tmp_path):
    code = tmp_path / "code"
    code.write_text("482913\n")
    pairing = Pairing(tmp_path / "token", code, window_s=-1)  # code works outside the window
    for _ in range(5):
        assert pairing.try_pair("000000") == (None, "invalid_code")
    assert pairing.try_pair("482913") == (None, "locked")
    pairing._locked_until = 0
    token, err = pairing.try_pair("482913")
    assert token and err is None
    assert oct((tmp_path / "token").stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI missing")
def test_certificate_is_stable(tmp_path):
    cert, key, fp1 = ensure_certificate(tmp_path, "ha-slate-test")
    _, _, fp2 = ensure_certificate(tmp_path, "ha-slate-test")
    assert fp1 == fp2 and len(fp1) == 64
    assert oct(key.stat().st_mode & 0o777) == "0o600"


# --- settings ------------------------------------------------------------------

def test_settings_validation_and_persistence(tmp_path):
    s = Settings()
    assert s.update({"volume": 3, "muted": 1}) == {"volume": 1.0, "muted": True}
    with pytest.raises(ValueError):
        s.update({"wake_word_location": "cloud"})
    with pytest.raises(ValueError):
        s.update({"kiosk_url": "file:///etc/passwd"})
    with pytest.raises(ValueError):
        s.update({"nope": 1})
    store = SettingsStore(tmp_path / "settings.json")
    store.save(s)
    assert store.load().volume == 1.0


# --- wake word store -----------------------------------------------------------

def test_builtin_models_present():
    models = WakeWordStore(Path("/nonexistent")).models()
    assert {"okay_nabu", "hey_jarvis", "alexa", "hey_mycroft"} <= set(models)
    assert models["okay_nabu"].type == TYPE_MICRO and models["okay_nabu"].builtin
    assert models["hey_rhasspy_oww"].type == TYPE_OWW


def test_install_list_remove_custom(tmp_path):
    store = WakeWordStore(tmp_path / "ww", builtin_dirs=[])
    sha = hashlib.sha256(FAKE_TFLITE).hexdigest()
    m = store.install("hey_slate", micro_config("Hey Slate"), FAKE_TFLITE, sha)
    assert (m.wake_word, m.type, m.probability_cutoff) == ("Hey Slate", TYPE_MICRO, 0.97)
    listed = store.models()["hey_slate"]
    assert listed.sha256 == sha and not listed.builtin
    assert json.loads((tmp_path / "ww" / "hey_slate.json").read_text())["model"] == "hey_slate.tflite"
    assert store.remove("hey_slate")
    assert "hey_slate" not in store.models()


def test_install_openwakeword_config(tmp_path):
    store = WakeWordStore(tmp_path, builtin_dirs=[])
    cfg = {"type": "openWakeWord", "wake_word": "Computer", "probability_cutoff": 0.6, "trained_languages": ["en"]}
    m = store.install("computer", cfg, FAKE_TFLITE, hashlib.sha256(FAKE_TFLITE).hexdigest())
    assert (m.type, m.probability_cutoff) == (TYPE_OWW, 0.6)


@pytest.mark.parametrize("model_id,config,data,sha,error", [
    ("Bad Id", micro_config("x"), FAKE_TFLITE, None, "invalid wake word id"),
    ("ok", micro_config("x"), FAKE_TFLITE, "0" * 64, "sha256 mismatch"),
    ("ok", micro_config("x"), b"not a model at all", None, "TensorFlow Lite"),
    ("ok", {"type": "micro", "wake_word": "x", "micro": {}}, FAKE_TFLITE, None, "probability_cutoff"),
    ("ok", {"type": "porcupine", "wake_word": "x"}, FAKE_TFLITE, None, "unsupported"),
    ("ok", {"type": "micro", "micro": {"probability_cutoff": 0.9}}, FAKE_TFLITE, None, "wake_word"),
])
def test_install_rejects(tmp_path, model_id, config, data, sha, error):
    store = WakeWordStore(tmp_path, builtin_dirs=[])
    with pytest.raises(WakeWordError, match=error):
        store.install(model_id, config, data, sha or hashlib.sha256(data).hexdigest())
    assert not list(tmp_path.glob("*.json"))


# --- engine thresholds (fake runner) -------------------------------------------

class ScriptedRunner:
    def __init__(self, script):
        self.script = list(script)

    def process(self, audio):
        return self.script.pop(0) if self.script else []

    def reset(self):
        pass


def _model(mid, cutoff):
    return WakeWordModel(mid, mid.title(), TYPE_MICRO, Path("/x"), probability_cutoff=cutoff)


def test_engine_threshold_refractory_and_best_match():
    t = [100.0]
    script = [[("a", 0.5)], [("a", 0.95), ("b", 0.99)], [("a", 0.99)]]
    eng = WakeWordEngine([_model("a", 0.9), _model("b", 0.9)], runner_factory=lambda m: ScriptedRunner(script),
                         clock=lambda: t[0])
    assert eng.process(b"") is None
    det = eng.process(b"")
    assert det.id == "b" and det.wake_word == "B"
    assert eng.process(b"") is None  # inside refractory window
    t[0] += 3
    eng.runner.script = [[("a", 0.99)]]
    assert eng.process(b"").id == "a"


def test_engine_sensitivity_presets():
    m = [_model("a", 0.97)]
    f = lambda models: ScriptedRunner([])  # noqa: E731
    assert WakeWordEngine(m, "more_sensitive", runner_factory=f).cutoffs["a"] < 0.97
    assert WakeWordEngine(m, "less_sensitive", runner_factory=f).cutoffs["a"] > 0.97


def test_engine_max_active():
    with pytest.raises(WakeWordError):
        WakeWordEngine([_model(str(i), 0.9) for i in range(MAX_ACTIVE + 1)], runner_factory=lambda m: None)


# --- real TFLite engines on synthesized speech ---------------------------------

def _espeak_16k(phrase: str, tmp_path: Path) -> bytes:
    wav = tmp_path / "s.wav"
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "130", "-w", str(wav), phrase], check=True)
    with wave.open(str(wav)) as w:
        rate, data = w.getframerate(), array.array("h", w.readframes(w.getnframes()))
    n = int(len(data) * 16000 / rate)
    out = array.array("h", (data[min(len(data) - 1, int(i * rate / 16000))] for i in range(n)))
    silence = array.array("h", [0] * 16000)
    return (silence + out + silence + silence).tobytes()


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed")
@pytest.mark.parametrize("phrase,expected", [("okay nabu", "okay_nabu"), ("turn on the kitchen lights", None)])
def test_real_microwakeword_detection(tmp_path, phrase, expected):
    models = WakeWordStore(Path("/nonexistent")).models()
    eng = WakeWordEngine([models["okay_nabu"], models["hey_jarvis_oww"]], "default")
    pcm = _espeak_16k(phrase, tmp_path)
    hits = [d.id for i in range(0, len(pcm), 640) if (d := eng.process(pcm[i:i + 640]))]
    assert hits == ([expected] if expected else [])
