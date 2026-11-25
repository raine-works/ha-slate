"""End-to-end: real agent, real TLS WebSocket server, scripted Home Assistant client."""

import asyncio
import hashlib
import json

import pytest
import websockets

from slate_agent import protocol
from slate_agent.agent import Agent, AgentPaths, VoiceState
from slate_agent.audio import NullPlayer, QueueSource
from slate_agent.security import Pairing, ensure_certificate, server_ssl_context
from slate_agent.wakeword import WakeWordStore

from .helpers import FAKE_TFLITE, WAKE, FakeEngine, FakeLightd, client_ssl, micro_config, recv_until

SILENCE = bytes(640)


class Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.sources: list[QueueSource] = []
        self.lightd = FakeLightd()
        self.player = NullPlayer()
        (tmp_path / "run").mkdir()
        self.agent = Agent(
            paths=AgentPaths(tmp_path / "state", tmp_path / "kiosk.env", tmp_path / "run"),
            device_id="aabbccddeeff", name="Test Slate",
            store=WakeWordStore(tmp_path / "state" / "wakewords"),
            pairing=Pairing(tmp_path / "state" / "token", window_s=60),
            source_factory=self._new_source, player=self.player, lightd=self.lightd,
            engine_factory=FakeEngine,
        )

    def _new_source(self):
        self.sources.append(QueueSource())
        return self.sources[-1]

    async def __aenter__(self):
        cert, key, self.fingerprint = ensure_certificate(self.tmp / "state", "test")
        await self.agent.start()
        self.server = await websockets.serve(self.agent.handle_connection, "127.0.0.1", 0,
                                             ssl=server_ssl_context(cert, key))
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.agent.stop()

    async def connect(self, token=None, code=None):
        ws = await websockets.connect(f"wss://127.0.0.1:{self.port}/v1", ssl=client_ssl())
        hello = json.loads(await ws.recv())
        if token:
            await ws.send(json.dumps({"type": "auth", "proof": protocol.proof(token, hello["nonce"])}))
        else:
            await ws.send(json.dumps({"type": "pair", "code": code, "client": "pytest"}))
        result = json.loads(await ws.recv())
        return ws, hello, result

    async def feed(self, *chunks):
        while not self.sources:
            await asyncio.sleep(0.01)
        for c in chunks:
            self.sources[-1].queue.put_nowait(c)
        await asyncio.sleep(0.05)


async def request(ws, msg, rid=[0]):
    rid[0] += 1
    await ws.send(json.dumps({**msg, "id": rid[0]}))
    res, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "result" and m["id"] == rid[0])
    return res


@pytest.mark.asyncio
async def test_pair_then_authenticate(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, hello, result = await rig.connect()
        assert hello["paired"] is False and hello["protocol"] == 1
        assert result["type"] == "auth-ok" and result["token"]
        state, _ = await recv_until(ws, lambda m: m.get("type") == "state")
        assert "okay_nabu" in {w["id"] for w in state["wake_words"]}
        assert state["max_active_wake_words"] == 2
        assert "zeroconf" in rig.lightd.events
        await ws.close()

        ws2, hello2, ok = await rig.connect(token=result["token"])
        assert hello2["paired"] is True and ok["type"] == "auth-ok" and "token" not in ok
        await ws2.close()

        ws3, _, bad = await rig.connect(token="wrong-token")
        assert bad == {"type": "auth-failed", "reason": "invalid_auth"}
        ws4, _, again = await rig.connect()  # second pairing attempt
        assert again["reason"] == "already_paired"
        for w in (ws3, ws4):
            await w.close()


@pytest.mark.asyncio
async def test_full_voice_turn(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")

        await rig.feed(SILENCE, WAKE, b"\x01" * 640, b"\x02" * 640)
        det, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "wake-word-detected")
        assert det["wake_word_id"] == "okay_nabu" and det["wake_word_phrase"] == "Okay Nabu"
        run, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run-pipeline")
        assert run["start_stage"] == "stt" and run["wake_word_phrase"] == "Okay Nabu"
        mic, _ = await recv_until(ws, lambda m: isinstance(m, bytes))
        assert mic[0] == protocol.CH_MIC and mic[1:] == b"\x01" * 640
        assert rig.player.played and rig.player.played[0][0] == 22050  # wake chime

        # HA: end of speech -> device stops streaming
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": run["run_id"], "event": "stt-vad-end"}))
        await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "audio-end")
        assert rig.agent.state is VoiceState.WAITING

        # HA: TTS streamed as PCM
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": run["run_id"], "event": "tts-end",
                                  "data": {"has_audio": True}}))
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": run["run_id"], "event": "run-end"}))
        await ws.send(json.dumps({"type": "audio-start", "purpose": "tts", "rate": 22050, "width": 2, "channels": 1}))
        await ws.send(protocol.frame(protocol.CH_SPK, b"\x10\x00" * 500))
        await ws.send(json.dumps({"type": "audio-stop", "purpose": "tts", "run_id": run["run_id"]}))
        played, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "played")
        assert played["purpose"] == "tts"
        assert rig.player.played[-1] == (22050, b"\x10\x00" * 500)
        assert rig.agent.state is VoiceState.IDLE
        ev = rig.lightd.events
        for expected in ("wake_word_detected", "listening", "thinking", "tts_speaking", "tts_finished", "idle"):
            assert expected in ev
        assert ev.index("listening") < ev.index("thinking") < ev.index("tts_speaking")
        await ws.close()


@pytest.mark.asyncio
async def test_start_conversation_and_errors(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")
        res = await request(ws, {"type": "start-listening", "conversation": True})
        assert res["ok"]
        run, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run-pipeline")
        assert run["start_stage"] == "stt" and run["conversation"] is True
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": run["run_id"], "event": "error",
                                  "data": {"code": "stt-no-text-recognized"}}))
        await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "audio-end")
        await asyncio.sleep(0.1)
        assert rig.agent.state is VoiceState.IDLE and "pipeline_error" in rig.lightd.events
        # stale event from an older run is ignored
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": 999, "event": "stt-vad-end"}))
        await ws.close()


@pytest.mark.asyncio
async def test_home_assistant_wake_word_mode_streams_continuously(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")
        res = await request(ws, {"type": "set-config", "settings": {"wake_word_location": "home_assistant"}})
        assert res["applied"] == {"wake_word_location": "home_assistant"}
        await rig.feed(b"\x05" * 640, b"\x06" * 640)
        run, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run-pipeline")
        assert run["start_stage"] == "wake_word"
        frames = [await recv_until(ws, lambda m: isinstance(m, bytes)) for _ in range(2)]
        assert [f[0][1:2] for f in frames] == [b"\x05", b"\x06"]
        await ws.close()


@pytest.mark.asyncio
async def test_config_mute_volume_lights_kiosk(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")
        res = await request(ws, {"type": "set-config", "settings": {
            "active_wake_words": ["hey_jarvis", "alexa"], "wake_word_sensitivity": "more_sensitive",
            "volume": 0.4, "muted": True, "kiosk_url": "http://ha.local:8123/wall"}})
        assert res["ok"] and set(res["applied"]) == {"active_wake_words", "wake_word_sensitivity", "volume",
                                                      "muted", "kiosk_url"}
        assert [m.id for m in rig.agent._engine.models] == ["hey_jarvis", "alexa"]
        assert rig.agent._engine.sensitivity == "more_sensitive"
        assert (tmp_path / "kiosk.env").read_text() == "KIOSK_URL=http://ha.local:8123/wall\n"
        assert json.loads((tmp_path / "state" / "settings.json").read_text())["muted"] is True
        # muted: wake word chunks are ignored and capture is stopped
        await rig.feed(WAKE)
        assert rig.agent.state is VoiceState.IDLE

        bad = await request(ws, {"type": "set-config", "settings": {"active_wake_words": ["nope"]}})
        assert not bad["ok"] and "unknown wake words" in bad["error"]
        too_many = await request(ws, {"type": "set-config", "settings": {"active_wake_words": ["alexa", "hey_jarvis", "okay_nabu"]}})
        assert not too_many["ok"]

        res = await request(ws, {"type": "light", "target": "edge", "state": True, "red": 1.0, "effect": "Breathe"})
        assert res["ok"] and rig.lightd.lights_state["edge"]["effect"] == "Breathe"
        state, _ = await recv_until(ws, lambda m: m.get("type") == "state")
        assert state["lights"]["edge"]["state"] is True

        assert (await request(ws, {"type": "reboot"}))["ok"]
        assert (tmp_path / "run" / "reboot").exists()
        await ws.close()


@pytest.mark.asyncio
async def test_custom_wake_word_upload_sync_and_activate(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")
        sha = hashlib.sha256(FAKE_TFLITE).hexdigest()

        res = await request(ws, {"type": "wake-word-sync", "models": [{"id": "hey_slate", "sha256": sha}]})
        assert res["missing"] == ["hey_slate"]
        assert (await request(ws, {"type": "wake-word-upload", "wake_word_id": "hey_slate", "sha256": sha,
                                   "size": len(FAKE_TFLITE), "config": micro_config("Hey Slate")}))["ok"]
        for i in range(0, len(FAKE_TFLITE), 16):
            await ws.send(protocol.frame(protocol.CH_FILE, FAKE_TFLITE[i:i + 16]))
        res = await request(ws, {"type": "wake-word-upload-end", "wake_word_id": "hey_slate"})
        assert res["ok"] and res["wake_word"]["wake_word"] == "Hey Slate"
        state, _ = await recv_until(ws, lambda m: m.get("type") == "state")
        assert {"id": "hey_slate", "builtin": False} .items() <= next(
            w for w in state["wake_words"] if w["id"] == "hey_slate").items()

        # Already in sync -> nothing missing
        assert (await request(ws, {"type": "wake-word-sync", "models": [{"id": "hey_slate", "sha256": sha}]}))["missing"] == []

        # Activate the custom model and say it
        assert (await request(ws, {"type": "set-config", "settings": {"active_wake_words": ["hey_slate"]}}))["ok"]
        await rig.feed(WAKE)
        det, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "wake-word-detected")
        assert det["wake_word_phrase"] == "Hey Slate"

        # Corrupt upload is rejected and leaves nothing behind
        await request(ws, {"type": "wake-word-upload", "wake_word_id": "broken", "sha256": sha,
                           "size": len(FAKE_TFLITE), "config": micro_config("Broken")})
        await ws.send(protocol.frame(protocol.CH_FILE, b"\x00" * len(FAKE_TFLITE)))
        res = await request(ws, {"type": "wake-word-upload-end", "wake_word_id": "broken"})
        assert not res["ok"] and "sha256" in res["error"]

        # Removed from HA's folder -> removed from the device
        res = await request(ws, {"type": "wake-word-sync", "models": []})
        assert res["removed"] == ["hey_slate"]
        assert not (tmp_path / "state" / "wakewords" / "hey_slate.tflite").exists()
        await ws.close()


@pytest.mark.asyncio
async def test_wake_without_session_signals_error(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.feed(WAKE)
        assert rig.lightd.events[-1] == "pipeline_error"
        assert rig.agent.state is VoiceState.IDLE


@pytest.mark.asyncio
async def test_home_assistant_wake_mode_backs_off_after_error(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _, _ = await rig.connect()
        await recv_until(ws, lambda m: m.get("type") == "state")
        await request(ws, {"type": "set-config", "settings": {"wake_word_location": "home_assistant"}})
        await rig.feed(SILENCE)
        run, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run-pipeline")
        await ws.send(json.dumps({"type": "pipeline-event", "run_id": run["run_id"], "event": "error",
                                  "data": {"code": "wake-engine-missing"}}))
        await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "audio-end")
        await rig.feed(*[SILENCE] * 20)
        await asyncio.sleep(0.2)
        assert rig.agent.run_id == run["run_id"]  # no new run during the back-off window
        rig.agent._ha_wake_retry_at = 0
        await rig.feed(SILENCE)
        nxt, _ = await recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run-pipeline")
        assert nxt["run_id"] == run["run_id"] + 1
        await ws.close()
