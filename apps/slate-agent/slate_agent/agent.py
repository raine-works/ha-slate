"""The device agent: one authenticated Home Assistant session + the voice loop.

Voice state machine::

    IDLE --wake word (device) / audio (HA wake mode)--> STREAMING
    STREAMING --stt-vad-end | stt-end | timeout--> WAITING
    WAITING --tts-end + audio-start--> RESPONDING --played--> IDLE
    WAITING --run-end (no TTS) | error--> IDLE
    IDLE --start-listening (start_conversation / continue)--> STREAMING
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import __version__, audio
from .lightd import LightdClient
from .protocol import CH_FILE, CH_MIC, CH_SPK, PROTOCOL_VERSION, encode, frame, verify_proof
from .security import Pairing
from .settings import Settings, SettingsStore
from .wakeword import MAX_ACTIVE, Detection, WakeWordEngine, WakeWordError, WakeWordStore

_LOG = logging.getLogger(__name__)

STREAM_TIMEOUT_S = 30.0     # hard cap on one utterance
RESPONSE_TIMEOUT_S = 20.0   # waiting for HA after the utterance ended
AUTH_TIMEOUT_S = 10.0
UPLOAD_TIMEOUT_S = 60.0
HA_WAKE_RETRY_S = 5.0       # back-off after a failed HA-side wake word run


class VoiceState(enum.StrEnum):
    IDLE = "idle"
    STREAMING = "streaming"
    WAITING = "waiting"
    RESPONDING = "responding"


@dataclass
class Upload:
    id: str
    sha256: str
    size: int
    config: dict[str, Any]
    data: bytearray = field(default_factory=bytearray)
    started: float = field(default_factory=time.monotonic)


@dataclass
class AgentPaths:
    state_dir: Path
    kiosk_env: Path | None = None        # written when HA changes the kiosk URL
    runtime_dir: Path | None = None      # trigger files for privileged actions


class Session:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send(encode(msg))

    async def send_bin(self, channel: int, payload: bytes) -> None:
        async with self._lock:
            await self.ws.send(frame(channel, payload))


class Agent:
    def __init__(
        self,
        *,
        paths: AgentPaths,
        device_id: str,
        name: str,
        store: WakeWordStore,
        pairing: Pairing,
        source_factory: Callable[[], audio.AudioSource],
        player: audio.Player,
        lightd: LightdClient,
        engine_factory: Callable[[list, str], WakeWordEngine] = WakeWordEngine,
        model: str = "HA Slate",
    ) -> None:
        self.paths = paths
        self.device_id = device_id
        self.name = name
        self.model = model
        self.store = store
        self.pairing = pairing
        self.source_factory = source_factory
        self.player = player
        self.lightd = lightd
        self.engine_factory = engine_factory
        self.settings_store = SettingsStore(paths.state_dir / "settings.json")
        self.settings: Settings = self.settings_store.load()

        self.session: Session | None = None
        self.state = VoiceState.IDLE
        self.run_id = 0
        self._state_since = time.monotonic()
        self._expect_tts = False
        self._ha_wake_retry_at = 0.0
        self._engine: WakeWordEngine | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wakeword")
        self._unmuted = asyncio.Event()
        self._play_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._upload: Upload | None = None
        self._tasks: list[asyncio.Task] = []
        self._capture_source: audio.AudioSource | None = None
        self.last_detection: Detection | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.reload_engine()
        if not self.settings.muted:
            self._unmuted.set()
        await self.lightd.voice_event("muted", {"muted": self.settings.muted})
        await self.lightd.voice_event("disconnected")
        self._tasks = [asyncio.create_task(self._capture_loop(), name="capture"),
                       asyncio.create_task(self._playback_loop(), name="playback"),
                       asyncio.create_task(self._watchdog_loop(), name="watchdog")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self._capture_source:
            await self._capture_source.close()
        await self.player.abort()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def reload_engine(self) -> None:
        models = self.store.models()
        active = [models[i] for i in self.settings.active_wake_words if i in models][:MAX_ACTIVE]
        missing = [i for i in self.settings.active_wake_words if i not in models]
        if missing:
            _LOG.warning("active wake words not installed: %s", missing)
        loop = asyncio.get_running_loop()
        sensitivity = self.settings.wake_word_sensitivity
        self._engine = await loop.run_in_executor(self._executor, self.engine_factory, active, sensitivity)
        _LOG.info("wake words active: %s (sensitivity %s)", [m.id for m in active], sensitivity)

    # ------------------------------------------------------------------ connection
    async def handle_connection(self, ws: Any, path: str | None = None) -> None:
        """websockets handler (compatible with the legacy and new APIs)."""
        session = Session(ws)
        nonce = secrets.token_hex(16)
        await session.send({"type": "hello", "protocol": PROTOCOL_VERSION, "device_id": self.device_id,
                            "name": self.name, "model": self.model, "sw_version": __version__,
                            "paired": self.pairing.paired, "nonce": nonce})
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S))
        except Exception:  # timeout, garbage, or client gone (e.g. a probe that only reads hello)
            return
        ok, extra = self._authenticate(first, nonce)
        if not ok:
            await session.send({"type": "auth-failed", **extra})
            await ws.close()
            return
        await session.send({"type": "auth-ok", **extra})

        previous, self.session = self.session, session
        if previous is not None:
            _LOG.info("new Home Assistant session replaces the previous one")
            with contextlib.suppress(Exception):
                await previous.ws.close()
        await self._reset_voice("new session")
        await self.send_state()
        await self.lightd.voice_event("zeroconf", {"status": "connected"})
        try:
            async for raw in ws:
                try:
                    if isinstance(raw, bytes):
                        await self._on_binary(raw)
                    else:
                        await self._on_message(json.loads(raw))
                except (ValueError, KeyError, TypeError) as exc:
                    _LOG.warning("bad message from HA: %s", exc)
        except Exception as exc:  # connection closed / reset (library-specific types)
            _LOG.info("session ended: %s", exc)
        finally:
            if self.session is session:
                self.session = None
                await self._reset_voice("disconnected")
                await self.lightd.voice_event("disconnected")

    def _authenticate(self, msg: dict[str, Any], nonce: str) -> tuple[bool, dict[str, Any]]:
        if msg.get("type") == "auth":
            token = self.pairing.token
            if token and verify_proof(token, nonce, msg.get("proof", "")):
                return True, {}
            return False, {"reason": "invalid_auth" if token else "not_paired"}
        if msg.get("type") == "pair":
            token, reason = self.pairing.try_pair(msg.get("code"))
            if token:
                _LOG.info("paired with %s", msg.get("client", "unknown client"))
                return True, {"token": token}
            return False, {"reason": reason}
        return False, {"reason": "expected_auth"}

    async def send_state(self) -> None:
        if self.session is None:
            return
        models = self.store.models()
        await self.session.send({
            "type": "state",
            "voice_state": self.state.value,
            "settings": self.settings.as_dict(),
            "wake_words": [m.describe() for m in models.values()],
            "max_active_wake_words": MAX_ACTIVE,
            "lights": await self.lightd.lights(),
            "sw_version": __version__,
        })

    async def _reply(self, msg: dict[str, Any], ok: bool = True, **data: Any) -> None:
        if "id" in msg and self.session is not None:
            await self.session.send({"type": "result", "id": msg["id"], "ok": ok, **data})

    # ------------------------------------------------------------------ HA -> device
    async def _on_message(self, msg: dict[str, Any]) -> None:
        typ = msg.get("type")
        handler = getattr(self, f"_msg_{str(typ).replace('-', '_')}", None)
        if handler is None:
            await self._reply(msg, False, error=f"unknown message {typ!r}")
            return
        try:
            await handler(msg)
        except (ValueError, WakeWordError) as exc:
            await self._reply(msg, False, error=str(exc))

    async def _msg_ping(self, msg: dict[str, Any]) -> None:
        await self._reply(msg)

    async def _msg_get_state(self, msg: dict[str, Any]) -> None:
        await self._reply(msg)
        await self.send_state()

    async def _msg_set_config(self, msg: dict[str, Any]) -> None:
        changes = dict(msg.get("settings", {}))
        if "active_wake_words" in changes:
            models = self.store.models()
            ids = [str(i) for i in changes["active_wake_words"]]
            unknown = [i for i in ids if i not in models]
            if unknown:
                raise ValueError(f"unknown wake words: {unknown}")
            if len(ids) > MAX_ACTIVE:
                raise ValueError(f"at most {MAX_ACTIVE} wake words")
        applied = self.settings.update(changes)
        self.settings_store.save(self.settings)
        if {"active_wake_words", "wake_word_sensitivity"} & applied.keys():
            await self.reload_engine()
        if "muted" in applied:
            await self._apply_mute()
        if "volume" in applied:
            await self.lightd.voice_event("volume_changed", {"volume": self.settings.volume})
        if "kiosk_url" in applied:
            self._write_kiosk_env()
        if "wake_word_location" in applied:
            await self._reset_voice("wake word location changed")
        await self._reply(msg, applied=applied)
        await self.send_state()

    async def _msg_light(self, msg: dict[str, Any]) -> None:
        target = msg.get("target")
        if target not in ("edge", "display"):
            raise ValueError("target must be edge or display")
        state = {k: msg[k] for k in ("state", "brightness", "red", "green", "blue", "effect") if k in msg}
        await self.lightd.set_light(target, state)
        await self._reply(msg)
        await self.send_state()

    async def _msg_pipeline_event(self, msg: dict[str, Any]) -> None:
        if msg.get("run_id") not in (None, self.run_id):
            return  # stale event from a superseded run
        event, data = msg.get("event"), msg.get("data") or {}
        if event == "wake_word-end" and data.get("wake_word_output"):
            await self.lightd.voice_event("wake_word_detected", {"wake_word_id": data["wake_word_output"].get("wake_word_id")})
            await self.lightd.voice_event("listening")
        elif event in ("stt-vad-end", "stt-end"):
            if self.state is VoiceState.STREAMING:
                await self._end_stream()
            await self.lightd.voice_event("thinking")
        elif event == "intent-start":
            await self.lightd.voice_event("thinking")
        elif event == "tts-end":
            self._expect_tts = bool(data.get("has_audio", True))
        elif event == "error":
            if self.settings.wake_word_location == "home_assistant":
                # A misconfigured pipeline fails instantly; don't restart it every 20 ms.
                self._ha_wake_retry_at = time.monotonic() + HA_WAKE_RETRY_S
            if self.state is VoiceState.STREAMING:
                await self._end_stream()
            await self.lightd.voice_event("pipeline_error", {"reason": data.get("code", "error")})
            self._set_state(VoiceState.IDLE)
            if data.get("code") not in ("wake-word-timeout", "duplicate_wake_up_detected"):
                await self._play_queue.put(("chime", "error"))
        elif event == "run-end":
            if self.state is VoiceState.STREAMING:
                await self._end_stream()
            if not self._expect_tts and self.state is not VoiceState.RESPONDING:
                self._set_state(VoiceState.IDLE)
                await self.lightd.voice_event("idle")

    async def _msg_audio_start(self, msg: dict[str, Any]) -> None:
        await self._play_queue.put(("start", msg))

    async def _msg_audio_stop(self, msg: dict[str, Any]) -> None:
        await self._play_queue.put(("stop", msg))

    async def _msg_start_listening(self, msg: dict[str, Any]) -> None:
        if self.settings.muted:
            raise ValueError("muted")
        await self._reply(msg)
        await self._start_run("stt", conversation=bool(msg.get("conversation", True)))

    async def _msg_stop(self, msg: dict[str, Any]) -> None:
        await self.player.abort()
        await self._reset_voice("stopped by Home Assistant")
        await self.lightd.voice_event("idle")
        await self._reply(msg)

    async def _msg_timer(self, msg: dict[str, Any]) -> None:
        ev = msg.get("event")
        data = {"id": msg.get("timer_id"), "name": msg.get("name") or "",
                "total_seconds": msg.get("total_seconds", 0), "seconds_left": msg.get("seconds_left", 0)}
        if ev == "started":
            await self.lightd.voice_event("timer_ticking", data)
        elif ev == "updated":
            await self.lightd.voice_event("timer_updated", data)
        elif ev == "cancelled":
            await self.lightd.voice_event("timer_updated", {**data, "seconds_left": 0})
        elif ev == "finished":
            await self.lightd.voice_event("timer_ringing", data)
            for _ in range(3):
                await self._play_queue.put(("chime", "done"))
            await self._play_queue.put(("event", "idle"))
        await self._reply(msg)

    # -- wake word management ------------------------------------------------
    async def _msg_wake_word_sync(self, msg: dict[str, Any]) -> None:
        """HA's custom_wake_words folder is the source of truth for custom models."""
        wanted = {str(m["id"]): str(m["sha256"]) for m in msg.get("models", [])}
        current = {m.id: m for m in self.store.models().values() if not m.builtin}
        missing = [i for i, sha in wanted.items() if i not in current or current[i].sha256 != sha]
        removed = [i for i in current if i not in wanted]
        for i in removed:
            self.store.remove(i)
        if removed and set(removed) & set(self.settings.active_wake_words):
            await self.reload_engine()
        await self._reply(msg, missing=missing, removed=removed)
        if removed:
            await self.send_state()

    async def _msg_wake_word_upload(self, msg: dict[str, Any]) -> None:
        size = int(msg["size"])
        if size <= 0 or size > 8 * 1024 * 1024:
            raise ValueError("invalid model size")
        self._upload = Upload(str(msg["wake_word_id"]), str(msg["sha256"]), size, dict(msg["config"]))
        await self._reply(msg)

    async def _msg_wake_word_upload_end(self, msg: dict[str, Any]) -> None:
        up, self._upload = self._upload, None
        if up is None or up.id != msg.get("wake_word_id"):
            raise ValueError("no matching upload in progress")
        if len(up.data) != up.size:
            raise ValueError(f"size mismatch: got {len(up.data)} of {up.size} bytes")
        model = self.store.install(up.id, up.config, bytes(up.data), up.sha256)
        _LOG.info("installed custom wake word %s (%s, %s)", model.id, model.wake_word, model.type)
        if model.id in self.settings.active_wake_words:
            await self.reload_engine()
        await self._reply(msg, wake_word=model.describe())
        await self.send_state()

    async def _msg_unpair(self, msg: dict[str, Any]) -> None:
        await self._reply(msg)
        self.pairing.unpair()
        if self.session:
            await self.session.ws.close()

    async def _msg_reboot(self, msg: dict[str, Any]) -> None:
        self._trigger("reboot")
        await self._reply(msg)

    async def _msg_reload_kiosk(self, msg: dict[str, Any]) -> None:
        self._trigger("reload-kiosk")
        await self._reply(msg)

    async def _on_binary(self, raw: bytes) -> None:
        if not raw:
            return
        channel, payload = raw[0], raw[1:]
        if channel == CH_SPK:
            await self._play_queue.put(("pcm", payload))
        elif channel == CH_FILE and self._upload is not None:
            if len(self._upload.data) + len(payload) > self._upload.size:
                self._upload = None
                raise ValueError("upload exceeds announced size")
            self._upload.data += payload

    # ------------------------------------------------------------------ voice loop
    async def _capture_loop(self) -> None:
        while True:
            await self._unmuted.wait()
            self._capture_source = self.source_factory()
            try:
                async for chunk in self._capture_source.chunks():
                    if self.settings.muted:
                        break
                    await self._on_audio(chunk)
            finally:
                await self._capture_source.close()
            if not self.settings.muted:
                await asyncio.sleep(1.0)  # capture died: back off, restart

    async def _on_audio(self, chunk: bytes) -> None:
        if self.state is VoiceState.STREAMING:
            if self.session is not None:
                await self.session.send_bin(CH_MIC, chunk)
            return
        if self.state is not VoiceState.IDLE:
            return
        if self.settings.wake_word_location == "home_assistant":
            if self.session is not None and time.monotonic() >= self._ha_wake_retry_at:
                await self._start_run("wake_word")
                await self.session.send_bin(CH_MIC, chunk)
            return
        engine = self._engine
        if engine is None:
            return
        det = await asyncio.get_running_loop().run_in_executor(self._executor, engine.process, chunk)
        if det is not None and self.state is VoiceState.IDLE:
            await self._on_wake(det)

    async def _on_wake(self, det: Detection) -> None:
        self.last_detection = det
        _LOG.info("wake word %s (%.2f)", det.id, det.probability)
        await self.lightd.voice_event("wake_word_detected", {"wake_word_id": det.id, "score": det.probability})
        if self.session is None:
            await self.lightd.voice_event("pipeline_error", {"reason": "not_connected"})
            await self._play_queue.put(("chime", "error"))
            return
        await self.session.send({"type": "wake-word-detected", "wake_word_id": det.id,
                                 "wake_word_phrase": det.wake_word, "probability": round(det.probability, 3)})
        if self.settings.wake_sound:
            await self._play_now(audio.chime("wake"))
        await self._start_run("stt", wake_word_id=det.id, wake_word_phrase=det.wake_word)

    async def _start_run(self, start_stage: str, **extra: Any) -> None:
        if self.session is None:
            return
        self.run_id += 1
        self._expect_tts = False
        self._set_state(VoiceState.STREAMING)
        await self.session.send({"type": "run-pipeline", "run_id": self.run_id, "start_stage": start_stage, **extra})
        if start_stage == "stt":
            await self.lightd.voice_event("listening")

    async def _end_stream(self) -> None:
        self._set_state(VoiceState.WAITING)
        if self.session is not None:
            await self.session.send({"type": "audio-end", "run_id": self.run_id})

    async def _reset_voice(self, reason: str) -> None:
        if self.state is VoiceState.STREAMING and self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.send({"type": "audio-end", "run_id": self.run_id})
        if self.state is not VoiceState.IDLE:
            _LOG.info("voice reset: %s", reason)
        self._expect_tts = False
        self._set_state(VoiceState.IDLE)

    def _set_state(self, state: VoiceState) -> None:
        if state is not self.state:
            self.state = state
            self._state_since = time.monotonic()

    async def _apply_mute(self) -> None:
        await self.lightd.voice_event("muted", {"muted": self.settings.muted})
        if self.settings.muted:
            self._unmuted.clear()
            await self._reset_voice("muted")
            if self._capture_source:
                await self._capture_source.close()  # microphone physically stops
        else:
            self._unmuted.set()

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            age = time.monotonic() - self._state_since
            if self.state is VoiceState.STREAMING and age > STREAM_TIMEOUT_S \
                    and self.settings.wake_word_location == "device":
                await self._end_stream()
            elif self.state is VoiceState.WAITING and age > RESPONSE_TIMEOUT_S:
                await self._reset_voice("no response from Home Assistant")
                await self.lightd.voice_event("pipeline_error", {"reason": "timeout"})
            if self._upload and time.monotonic() - self._upload.started > UPLOAD_TIMEOUT_S:
                self._upload = None

    # ------------------------------------------------------------------ playback
    async def _play_now(self, pcm: bytes, rate: int = 22050) -> None:
        await self.player.start(rate, 2, 1, self.settings.volume)
        await self.player.write(pcm)
        await self.player.finish()

    async def _playback_loop(self) -> None:
        purpose = None
        while True:
            kind, value = await self._play_queue.get()
            try:
                if kind == "start":
                    purpose = value.get("purpose", "tts")
                    self._set_state(VoiceState.RESPONDING)
                    await self.lightd.voice_event("tts_speaking")
                    await self.player.start(int(value["rate"]), int(value.get("width", 2)),
                                            int(value.get("channels", 1)), self.settings.volume)
                elif kind == "pcm" and purpose is not None:
                    await self.player.write(value)
                elif kind == "stop" and purpose is not None:
                    await self.player.finish()
                    finished, purpose = purpose, None
                    self._expect_tts = False
                    if self.state is VoiceState.RESPONDING:
                        self._set_state(VoiceState.IDLE)
                    await self.lightd.voice_event("tts_finished")
                    await self.lightd.voice_event("idle")
                    if self.session is not None:
                        await self.session.send({"type": "played", "purpose": finished,
                                                 "run_id": value.get("run_id", self.run_id)})
                elif kind == "chime":
                    await self._play_now(audio.chime(value))
                elif kind == "event":
                    await self.lightd.voice_event(value)
            except (OSError, ValueError, KeyError) as exc:
                _LOG.warning("playback failed: %s", exc)
                purpose = None

    # ------------------------------------------------------------------ privileged actions
    def _write_kiosk_env(self) -> None:
        if self.paths.kiosk_env is None:
            return
        url = self.settings.kiosk_url
        content = f"KIOSK_URL={url}\n" if url else ""
        tmp = self.paths.kiosk_env.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(self.paths.kiosk_env)  # slate-kiosk-reload.path restarts the browser

    def _trigger(self, name: str) -> None:
        if self.paths.runtime_dir is None:
            raise ValueError("not supported on this device")
        (self.paths.runtime_dir / name).write_text(str(time.time()))
