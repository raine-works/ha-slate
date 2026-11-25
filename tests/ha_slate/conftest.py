"""Fixtures: a real slate-agent (TLS WebSocket) running inside the HA test loop."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "slate-agent"))

import websockets  # noqa: E402

from slate_agent.agent import Agent, AgentPaths  # noqa: E402
from slate_agent.audio import NullPlayer, QueueSource  # noqa: E402
from slate_agent.security import Pairing, ensure_certificate, server_ssl_context  # noqa: E402
from slate_agent.wakeword import Detection, WakeWordStore  # noqa: E402

FAKE_TFLITE = b"\x1c\x00\x00\x00TFL3" + bytes(64)
WAKE = b"\x7f\x7f" * 320


def micro_config(phrase: str) -> dict:
    return {"type": "micro", "wake_word": phrase, "trained_languages": ["en"],
            "micro": {"probability_cutoff": 0.97, "sliding_window_size": 5}}


class FakeEngine:
    def __init__(self, models, sensitivity):
        self.models, self.sensitivity = models, sensitivity

    def process(self, chunk):
        if chunk == WAKE and self.models:
            return Detection(self.models[0].id, self.models[0].wake_word, 0.99)
        return None


class FakeLightd:
    def __init__(self):
        self.events: list[str] = []
        self.state = {"edge": {"state": False, "brightness": 1.0, "red": 1.0, "green": 1.0, "blue": 1.0,
                               "effect": "None"},
                      "display": {"state": True, "brightness": 1.0}}

    async def voice_event(self, event, data=None):
        self.events.append(event)

    async def set_light(self, target, state):
        self.state[target].update(state)
        return self.state[target]

    async def lights(self):
        return json.loads(json.dumps(self.state))


class AgentRig:
    """A panel on 127.0.0.1 with fake audio hardware."""

    def __init__(self, tmp_path: Path, pairing_code: str | None = None) -> None:
        builtin = tmp_path / "builtin"
        builtin.mkdir(parents=True)
        for mid, phrase in (("okay_nabu", "Okay Nabu"), ("hey_jarvis", "Hey Jarvis")):
            (builtin / f"{mid}.tflite").write_bytes(FAKE_TFLITE)
            (builtin / f"{mid}.json").write_text(json.dumps({**micro_config(phrase), "model": f"{mid}.tflite"}))
        code_file = None
        if pairing_code:
            code_file = tmp_path / "code"
            code_file.write_text(pairing_code)
        self.tmp = tmp_path
        self.lightd = FakeLightd()
        self.player = NullPlayer()
        self.sources: list[QueueSource] = []
        (tmp_path / "run").mkdir()
        self.agent = Agent(
            paths=AgentPaths(tmp_path / "state", tmp_path / "kiosk.env", tmp_path / "run"),
            device_id="02aabbccddee", name="Kitchen Slate",
            store=WakeWordStore(tmp_path / "state" / "wakewords", builtin_dirs=[(builtin, "micro")]),
            pairing=Pairing(tmp_path / "state" / "token", code_file, window_s=600),
            source_factory=self._source, player=self.player, lightd=self.lightd, engine_factory=FakeEngine,
        )

    def _source(self):
        self.sources.append(QueueSource())
        return self.sources[-1]

    async def start(self) -> None:
        cert, key, self.fingerprint = ensure_certificate(self.tmp / "state", "test")
        await self.agent.start()
        self.server = await websockets.serve(self.agent.handle_connection, "127.0.0.1", 0,
                                             ssl=server_ssl_context(cert, key))
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        await self.agent.stop()

    async def feed(self, *chunks: bytes) -> None:
        while not self.sources:
            await asyncio.sleep(0.01)
        for c in chunks:
            self.sources[-1].queue.put_nowait(c)

    async def drop_session(self) -> None:
        if self.agent.session:
            await self.agent.session.ws.close()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def core_components(hass):
    """conversation (via assist_pipeline) needs the core component's exposed entities."""
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
async def rig(hass, socket_enabled, tmp_path):
    # Depends on hass so it is torn down before HA's lingering-task checks.
    r = AgentRig(tmp_path / "panel")
    await r.start()
    yield r
    await r.stop()
