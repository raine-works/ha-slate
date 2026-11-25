"""Wake word model store and on-device detection engine.

Model format follows Home Assistant's ``custom_wake_words`` convention (the
same files ESPHome satellites use): ``<id>.json`` + ``<id>.tflite``.

microWakeWord (``"type": "micro"``)::

    {"type": "micro", "wake_word": "Hey Slate", "model": "hey_slate.tflite",
     "trained_languages": ["en"], "version": 2,
     "micro": {"probability_cutoff": 0.97, "sliding_window_size": 5, ...}}

openWakeWord (``"type": "openWakeWord"``, Slate extension; the ``.tflite`` is
what the openWakeWord training notebook exports)::

    {"type": "openWakeWord", "wake_word": "Hey Slate", "model": "hey_slate.tflite",
     "trained_languages": ["en"], "probability_cutoff": 0.5}

Built-in models come from the pymicro-wakeword / pyopen-wakeword packages.
Custom models are installed over the protocol by the HA integration and stored
under ``custom_dir``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Callable

_LOG = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
MAX_MODEL_BYTES = 8 * 1024 * 1024
TYPE_MICRO = "micro"
TYPE_OWW = "openWakeWord"
REFRACTORY_S = 2.0
MAX_ACTIVE = 2

# Sensitivity presets adjust each model's own probability cutoff.
_SENSITIVITY = {
    "default": lambda c: c,
    "more_sensitive": lambda c: max(0.05, c - min(0.1, 1.0 - c)),
    "less_sensitive": lambda c: min(0.995, c + (1.0 - c) * 0.5),
}


class WakeWordError(ValueError):
    """A wake word model or its config was refused; the message says why."""


def normalize_type(value: str) -> str:
    """Map the spellings of a model type to ``TYPE_MICRO`` or ``TYPE_OWW``; else raise ``WakeWordError``."""
    v = str(value).strip().lower()
    if v in ("micro", "microwakeword", "mww"):
        return TYPE_MICRO
    if v in ("openwakeword", "oww"):
        return TYPE_OWW
    raise WakeWordError(f"unsupported wake word type {value!r}")


@dataclass
class WakeWordModel:
    """One installed wake word model: its config, where the model file is, and whether it's built in."""

    id: str
    wake_word: str
    type: str
    model_path: Path
    trained_languages: list[str] = field(default_factory=list)
    probability_cutoff: float = 0.5
    sliding_window_size: int = 5
    builtin: bool = False
    sha256: str | None = None

    def describe(self) -> dict[str, Any]:
        """The model as reported to Home Assistant (no paths or tuning)."""
        return {"id": self.id, "wake_word": self.wake_word, "type": self.type,
                "trained_languages": self.trained_languages, "builtin": self.builtin,
                "sha256": self.sha256}


def parse_config(model_id: str, config: dict[str, Any], model_path: Path) -> WakeWordModel:
    """Validate a model's JSON config (the format of Home Assistant's wake word folder) into a model.

    microWakeWord models need ``micro.probability_cutoff``; openWakeWord ones default to 0.5.
    Raises ``WakeWordError`` for a missing wake word, an unknown type or a cutoff outside (0, 1),
    and ``ValueError`` or ``TypeError`` for an openWakeWord cutoff that isn't a number.
    """
    if not isinstance(config, dict):
        raise WakeWordError("config must be a JSON object")
    if not str(config.get("wake_word", "")).strip():
        raise WakeWordError("config.wake_word is required")
    typ = normalize_type(config.get("type", ""))
    langs = [str(x) for x in config.get("trained_languages", [])]
    if typ == TYPE_MICRO:
        micro = config.get("micro") or {}
        try:
            cutoff = float(micro["probability_cutoff"])
            window = int(micro.get("sliding_window_size", 5))
        except (KeyError, TypeError, ValueError) as exc:
            raise WakeWordError("micro.probability_cutoff is required") from exc
    else:
        cutoff = float(config.get("probability_cutoff", 0.5))
        window = 1
    if not 0.0 < cutoff < 1.0:
        raise WakeWordError("probability_cutoff must be in (0, 1)")
    return WakeWordModel(model_id, str(config["wake_word"]).strip(), typ, model_path, langs, cutoff, window)


def _check_tflite(data: bytes) -> None:
    # FlatBuffer: 4-byte root offset, then the "TFL3" file identifier.
    if len(data) < 8 or data[4:8] != b"TFL3":
        raise WakeWordError("model is not a TensorFlow Lite flatbuffer")


class WakeWordStore:
    """The wake word models available on the panel: the packages' built-ins plus custom ones in ``custom_dir``."""

    def __init__(self, custom_dir: Path, builtin_dirs: list[tuple[Path, str]] | None = None) -> None:
        """Use ``custom_dir`` for installed models (created on first install).

        ``builtin_dirs`` lists ``(directory, type)`` pairs to scan for built-ins;
        by default, the model folders of pymicro-wakeword and pyopen-wakeword.
        """
        self.custom_dir = custom_dir
        self.builtin_dirs = builtin_dirs if builtin_dirs is not None else _default_builtin_dirs()

    # -- listing --------------------------------------------------------------
    def models(self) -> dict[str, WakeWordModel]:
        """Every usable model by ID; a custom model replaces a built-in with the same ID.

        Unreadable models, and those ``parse_config`` refuses with ``ValueError``
        or ``WakeWordError``, are skipped (custom ones with a warning).
        """
        out: dict[str, WakeWordModel] = {}
        for directory, kind in self.builtin_dirs:
            for m in _scan_builtin(directory, kind):
                out.setdefault(m.id, m)
        for cfg in sorted(self.custom_dir.glob("*.json")) if self.custom_dir.exists() else []:
            model_path = cfg.with_suffix(".tflite")
            try:
                config = json.loads(cfg.read_text())
                m = parse_config(cfg.stem, config, model_path)
                m.sha256 = _sha256_file(model_path)
            except (OSError, ValueError) as exc:
                _LOG.warning("skipping custom wake word %s: %s", cfg.name, exc)
                continue
            out[m.id] = m  # custom overrides a builtin with the same id
        return out

    # -- install / remove -----------------------------------------------------
    def install(self, model_id: str, config: dict[str, Any], model: bytes, sha256: str) -> WakeWordModel:
        """Validate and save a custom model, replacing any with the same ID; returns it.

        Raises ``WakeWordError`` for an invalid ID, a model over ``MAX_MODEL_BYTES``,
        a ``sha256`` (hex) that doesn't match, a file that isn't TFLite, or a bad
        config (see ``parse_config``); ``OSError`` if it can't be written. The
        config's ``model`` field is overwritten to point at the saved file.
        """
        if not ID_RE.match(model_id):
            raise WakeWordError("invalid wake word id")
        if len(model) > MAX_MODEL_BYTES:
            raise WakeWordError("model too large")
        if hashlib.sha256(model).hexdigest() != sha256:
            raise WakeWordError("sha256 mismatch")
        _check_tflite(model)
        config = dict(config)
        config["model"] = f"{model_id}.tflite"
        parsed = parse_config(model_id, config, self.custom_dir / f"{model_id}.tflite")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        # Model first, config last: a config without its model is never visible.
        _atomic_write(self.custom_dir / f"{model_id}.tflite", model)
        _atomic_write(self.custom_dir / f"{model_id}.json", json.dumps(config, indent=1).encode())
        parsed.sha256 = sha256
        return parsed

    def remove(self, model_id: str) -> bool:
        """Delete custom model ``model_id``; returns whether any of its files existed.

        Built-ins can't be removed. Raises ``WakeWordError`` for an invalid ID.
        """
        if not ID_RE.match(model_id):
            raise WakeWordError("invalid wake word id")
        removed = False
        for suffix in (".json", ".tflite"):
            try:
                (self.custom_dir / f"{model_id}{suffix}").unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


def _scan_builtin(directory: Path, kind: str) -> list[WakeWordModel]:
    out: list[WakeWordModel] = []
    if not directory.exists():
        return out
    if kind == TYPE_MICRO:
        for cfg in sorted(directory.glob("*.json")):
            try:
                config = json.loads(cfg.read_text())
                out.append(parse_config(cfg.stem, config, directory / config["model"]))
                out[-1].builtin = True
            except (OSError, ValueError, KeyError):
                continue
    else:
        for tfl in sorted(directory.glob("*.tflite")):
            if tfl.stem in ("melspectrogram", "embedding_model"):
                continue
            # Same phrase as the micro model -> distinct id, micro stays the default.
            out.append(WakeWordModel(f"{tfl.stem}_oww", tfl.stem.replace("_", " ").title(),
                                     TYPE_OWW, tfl, ["en"], 0.5, 1, builtin=True))
    return out


def _default_builtin_dirs() -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    for module, kind in (("pymicro_wakeword", TYPE_MICRO), ("pyopen_wakeword", TYPE_OWW)):
        try:
            mod = __import__(module)
            dirs.append((Path(mod.__file__).parent / "models", kind))
        except ImportError:
            _LOG.warning("%s not installed; its built-in wake words are unavailable", module)
    return dirs


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# -----------------------------------------------------------------------------
# Detection
# -----------------------------------------------------------------------------

@dataclass
class Detection:
    """A wake word heard: the model's ID, its phrase and the probability that crossed the cutoff."""

    id: str
    wake_word: str
    probability: float


class _Runner(Protocol):
    def process(self, audio: bytes) -> list[tuple[str, float]]: ...  # (model id, probability)
    def reset(self) -> None: ...


class TfliteRunner:
    """Runs any mix of microWakeWord and openWakeWord models on one audio stream.

    Feature front-ends are shared: one MicroWakeWordFeatures for all micro
    models, one mel+embedding OpenWakeWordFeatures for all openWakeWord models.
    """

    def __init__(self, models: list[WakeWordModel]) -> None:
        """Load ``models``; raises ``ImportError`` if a needed wake word package isn't installed."""
        self.micro: list[tuple[str, Any]] = []
        self.oww: list[tuple[str, Any]] = []
        self.micro_features = self.oww_features = None
        micro_models = [m for m in models if m.type == TYPE_MICRO]
        oww_models = [m for m in models if m.type == TYPE_OWW]
        if micro_models:
            from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
            from pymicro_wakeword.microwakeword import _MODULE_LIB_DIR
            lib = next(iter(_MODULE_LIB_DIR.glob("*tensorflowlite_c.*")))
            self.micro_features = MicroWakeWordFeatures()
            for m in micro_models:
                # Cutoff 0 here: thresholding happens in WakeWordEngine so the
                # sensitivity preset applies uniformly to both model types.
                self.micro.append((m.id, MicroWakeWord(
                    m.id, wake_word=m.wake_word, tflite_model=m.model_path, probability_cutoff=0.0,
                    sliding_window_size=m.sliding_window_size, trained_languages=m.trained_languages,
                    libtensorflowlite_c_path=lib)))
        if oww_models:
            from pyopen_wakeword import OpenWakeWord, OpenWakeWordFeatures
            self.oww_features = OpenWakeWordFeatures.from_builtin()
            for m in oww_models:
                self.oww.append((m.id, OpenWakeWord.from_model(m.model_path)))

    def process(self, audio: bytes) -> list[tuple[str, float]]:
        """Feed 16 kHz mono s16le ``audio``; returns ``(model id, probability)`` for each window it completed."""
        out: list[tuple[str, float]] = []
        if self.micro_features is not None:
            for features in self.micro_features.process_streaming(audio):
                for mid, model in self.micro:
                    p = model.process_streaming_prob(features)
                    if p is not None:
                        out.append((mid, p))
        if self.oww_features is not None:
            for embeddings in self.oww_features.process_streaming(audio):
                for mid, model in self.oww:
                    for p in model.process_streaming(embeddings):
                        out.append((mid, p))
        return out

    def reset(self) -> None:
        """Clear every model's streaming state (e.g. after a voice turn)."""
        for _, model in self.micro + self.oww:
            model.reset()


class WakeWordEngine:
    """Detects the active wake words in the microphone stream, at most once per ``REFRACTORY_S``."""

    def __init__(self, models: list[WakeWordModel], sensitivity: str = "default",
                 runner_factory: Callable[[list[WakeWordModel]], _Runner] = TfliteRunner,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """Listen for ``models`` with each cutoff adjusted by ``sensitivity`` (unknown values mean default).

        ``runner_factory`` and ``clock`` are seams for tests. With no models it
        detects nothing. Raises ``WakeWordError`` for more than ``MAX_ACTIVE`` models.
        """
        if len(models) > MAX_ACTIVE:
            raise WakeWordError(f"at most {MAX_ACTIVE} wake words can be active")
        adjust = _SENSITIVITY.get(sensitivity, _SENSITIVITY["default"])
        self.models = {m.id: m for m in models}
        self.cutoffs = {m.id: adjust(m.probability_cutoff) for m in models}
        self.runner = runner_factory(models) if models else None
        self.clock = clock
        self._blocked_until = 0.0

    def process(self, audio: bytes) -> Detection | None:
        """Feed a chunk of audio; returns the most probable detection, or None (also within the refractory period)."""
        if self.runner is None:
            return None
        best: Detection | None = None
        for mid, prob in self.runner.process(audio):
            if prob > self.cutoffs[mid] and (best is None or prob > best.probability):
                best = Detection(mid, self.models[mid].wake_word, prob)
        if best is None:
            return None
        now = self.clock()
        if now < self._blocked_until:
            return None
        self._blocked_until = now + REFRACTORY_S
        return best

    def reset(self) -> None:
        """Clear the models' streaming state; the refractory period is kept."""
        if self.runner is not None:
            self.runner.reset()
