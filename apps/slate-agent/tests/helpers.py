"""Shared fakes for agent tests."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from typing import Any

# Minimal bytes that pass the flatbuffer identifier check ("TFL3" at offset 4).
FAKE_TFLITE = b"\x1c\x00\x00\x00TFL3" + bytes(64)

WAKE = b"\x7f\x7f" * 320  # a chunk the fake engine treats as "wake word spoken"


def micro_config(phrase: str) -> dict[str, Any]:
    return {"type": "micro", "wake_word": phrase, "trained_languages": ["en"], "version": 2,
            "micro": {"probability_cutoff": 0.97, "sliding_window_size": 5,
                      "feature_step_size": 10, "tensor_arena_size": 30000}}


class FakeEngine:
    def __init__(self, models, sensitivity):
        self.models = models
        self.sensitivity = sensitivity

    def process(self, chunk):
        from slate_agent.wakeword import Detection
        if chunk == WAKE and self.models:
            return Detection(self.models[0].id, self.models[0].wake_word, 0.99)
        return None


class FakeLightd:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lights_state: dict[str, Any] = {"edge": {"state": False}, "display": {"state": True, "brightness": 1.0}}

    async def voice_event(self, event: str, data: dict | None = None) -> None:
        self.events.append(event)

    async def set_light(self, target: str, state: dict[str, Any]):
        self.lights_state.setdefault(target, {}).update(state)
        return self.lights_state[target]

    async def lights(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.lights_state))


def client_ssl() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the real client pins the fingerprint instead
    return ctx


async def recv_until(ws, predicate, timeout: float = 3.0):
    """Receive messages until predicate(msg) is true; returns (match, skipped)."""
    skipped = []
    async with asyncio.timeout(timeout):
        while True:
            raw = await ws.recv()
            msg = raw if isinstance(raw, bytes) else json.loads(raw)
            if predicate(msg):
                return msg, skipped
            skipped.append(msg)


def tmpdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
