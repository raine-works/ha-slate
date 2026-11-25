"""Local control socket: the only way into slate-lightd.

Protocol: one JSON object per line on a Unix stream socket; one reply line each.

    {"voice_event": {"event": "listening", "data": {}}}        from slate-agent
    {"light": {"target": "edge", "state": true, "brightness": 0.6,
               "red": 1.0, "green": 0.5, "blue": 0.0, "effect": "Breathe"}}
    {"light": {"target": "display", "state": true, "brightness": 0.4}}
    {"claim": "system", "effect": "progress", "color": [0, 80, 255], "value": 0.42}
    {"release": "system"}
    {"status": true}   -> {"active": {...} | null, "claims": [...], "lights": {...}}

The socket is created 0660 in /run/slate-lightd; the systemd unit gives the
group to slate-agent's group so the agent (and root services such as RAUC
hooks) can drive the bar and nothing else can.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from collections.abc import Callable

from .bridge import VoiceBridge
from .color import from_bytes
from .engine import Engine

_LOG = logging.getLogger(__name__)

EDGE_DEFAULT = {"state": False, "brightness": 1.0, "red": 1.0, "green": 1.0, "blue": 1.0, "effect": "None"}
DISPLAY_DEFAULT = {"state": True, "brightness": 1.0}


class LightController:
    """User-facing light state (persisted) on top of the claim engine."""

    def __init__(self, engine: Engine, bridge: VoiceBridge, backlight: Any | None,
                 state: dict[str, Any], save: Callable[[dict[str, Any]], None]) -> None:
        self.engine = engine
        self.bridge = bridge
        self.backlight = backlight
        self.state = state
        self.save = save
        self.state.setdefault("edge", dict(EDGE_DEFAULT))
        if backlight is not None:
            self.state.setdefault("display", dict(DISPLAY_DEFAULT))
        self.bridge.apply_user_light(self.state["edge"])
        if backlight is not None:
            backlight.apply(self.state["display"])

    def set_light(self, target: str, changes: dict[str, Any]) -> dict[str, Any]:
        if target == "edge":
            current = self.state["edge"]
            current.update({k: v for k, v in changes.items() if k in EDGE_DEFAULT})
            self.bridge.apply_user_light(current)
        elif target == "display":
            if self.backlight is None:
                raise ValueError("no backlight on this device")
            current = self.state["display"]
            current.update({k: v for k, v in changes.items() if k in DISPLAY_DEFAULT})
            self.backlight.apply(current)
        else:
            raise ValueError("target must be edge or display")
        self.save(self.state)
        return current

    def lights(self) -> dict[str, Any]:
        return {k: v for k, v in self.state.items() if k in ("edge", "display")}


def apply(engine: Engine, req: dict[str, Any], controller: LightController | None = None) -> dict[str, Any]:
    """Carry out one control socket request and return its JSON reply.

    ``req`` holds one of ``voice_event``, ``light``, ``claim``, ``release`` or ``status``;
    anything else gets ``{"ok": False, ...}``. ``voice_event`` and ``light`` need a
    ``controller``. Malformed requests raise ``ValueError``, ``TypeError`` or ``KeyError``,
    which ``serve`` turns into an error reply.
    """
    if "voice_event" in req:
        if controller is None:
            raise ValueError("voice events not supported")
        controller.bridge.handle(dict(req["voice_event"]))
        return {"ok": True}
    if "light" in req:
        if controller is None:
            raise ValueError("lights not supported")
        light = dict(req["light"])
        return {"ok": True, "state": controller.set_light(str(light.pop("target", "")), light)}
    if "release" in req:
        engine.release(str(req["release"]))
        return {"ok": True}
    if "claim" in req:
        engine.claim(
            str(req["claim"]),
            str(req.get("effect", "solid")),
            from_bytes(req.get("color", [255, 255, 255])),
            brightness=float(req.get("brightness", 1.0)),
            value=float(req.get("value", 0.0)),
            ttl=req.get("ttl"),
            priority=req.get("priority"),
            restart=bool(req.get("restart", False)),
        )
        return {"ok": True}
    if req.get("status"):
        top = engine.active()
        return {
            "active": None if top is None else {"source": top.source, "effect": top.effect, "priority": top.priority},
            "claims": sorted(engine.claims),
            "lights": controller.lights() if controller else {},
        }
    return {"ok": False, "error": "expected voice_event, light, claim, release or status"}


async def serve(engine: Engine, path: str, controller: LightController | None = None) -> asyncio.AbstractServer:
    """Listen on the Unix socket ``path`` for newline-delimited JSON requests (see ``apply``).

    A stale socket file is replaced, and the new one is made group-writable (0660) so only
    the daemon's group can drive the lights. Returns the running server; the caller closes it.
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    resp = apply(engine, json.loads(line), controller)
                except (ValueError, TypeError, KeyError) as exc:
                    resp = {"ok": False, "error": str(exc)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(handler, path)
    os.chmod(path, 0o660)
    _LOG.info("control socket at %s", path)
    return server
