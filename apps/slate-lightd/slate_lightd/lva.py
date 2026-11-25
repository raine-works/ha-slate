"""Client for the Linux Voice Assistant (LVA) peripheral WebSocket API.

LVA (github.com/OHF-Voice/linux-voice-assistant) is the voice service. It
exposes ``ws://<host>:6055`` for LED/button peripherals: it pushes pipeline
events, and lets a peripheral register Light entities that Home Assistant then
shows on the satellite's ESPHome device page. That gives HA control of the edge
bar and the display backlight with no MQTT broker and no extra integration.

Registrations are only enumerated by HA if they arrive within LVA's
``--peripheral-startup-wait`` window, so slate-lightd is ordered *before*
slate-voice.service and reconnects aggressively during startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from .color import RGB
from .effects import USER_EFFECT_MAP, USER_EFFECTS
from .engine import Engine

_LOG = logging.getLogger(__name__)

RED: RGB = (1.0, 0.0, 0.0)
AMBER: RGB = (1.0, 0.45, 0.0)
WHITE: RGB = (1.0, 1.0, 1.0)

EDGE_ID = "edge_light"
DISPLAY_ID = "display"


class VoiceBridge:
    """Maps LVA events onto engine claims. Transport-agnostic for testability."""

    def __init__(self, engine: Engine, accent: RGB, on_user_light: Callable[[dict[str, Any]], None],
                 on_display: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.engine = engine
        self.accent = accent
        self.on_user_light = on_user_light
        self.on_display = on_display
        self.timers: dict[str, tuple[float, float]] = {}  # id -> (deadline, total)

    def registrations(self) -> list[dict[str, Any]]:
        regs = [{"command": "register_light", "data": {
            "name": "Edge Light", "object_id": EDGE_ID, "effects": USER_EFFECTS,
            "supports_rgb": True, "supports_brightness": True}}]
        if self.on_display is not None:
            regs.append({"command": "register_light", "data": {
                "name": "Display", "object_id": DISPLAY_ID, "effects": [],
                "supports_rgb": False, "supports_brightness": True}})
        return regs

    def handle(self, msg: dict[str, Any]) -> None:
        ev = msg.get("event")
        data = msg.get("data") or {}
        e = self.engine
        if ev == "snapshot":
            self._muted(bool(data.get("muted")))
            if data.get("ha_connected") is False:
                e.claim("network", "breathe", RED, brightness=0.4)
            else:
                e.release("network")
        elif ev == "wake_word_detected":
            e.claim("voice", "grow", self.accent)
        elif ev == "listening":
            e.claim("voice", "solid", self.accent)
        elif ev == "thinking":
            e.claim("voice", "scan", self.accent)
        elif ev == "tts_speaking":
            e.claim("voice", "pulse", self.accent)
        elif ev in ("tts_finished", "idle"):
            e.release("voice")
            if ev == "idle":
                e.release("alarm")
        elif ev == "pipeline_error":
            e.release("voice")
            e.claim("error", "flash", RED, ttl=1.3)
        elif ev == "disconnected":
            e.release("voice")
            e.claim("network", "breathe", RED, brightness=0.4)
        elif ev == "zeroconf":
            if data.get("status") == "connected":
                e.release("network")
        elif ev == "muted":
            self._muted(bool(data.get("muted", True)))
        elif ev in ("timer_ticking", "timer_updated"):
            left, total = float(data.get("seconds_left", 0)), float(data.get("total_seconds", 0))
            if left <= 0 or total <= 0:
                self.timers.pop(str(data.get("id")), None)
            else:
                self.timers[str(data.get("id"))] = (time.monotonic() + left, total)
            self.tick_timers()
        elif ev == "timer_ringing":
            self.timers.pop(str(data.get("id")), None)
            self.tick_timers()
            e.claim("alarm", "pulse", AMBER)
        elif ev == "volume_changed":
            e.claim("media:volume", "progress", WHITE, brightness=0.6, value=float(data.get("volume", 0)), ttl=1.5)
        elif ev == "light_command":
            if data.get("object_id") == EDGE_ID:
                self.on_user_light(data)
            elif data.get("object_id") == DISPLAY_ID and self.on_display is not None:
                self.on_display(data)

    def tick_timers(self) -> None:
        """Show the soonest-expiring timer as a draining bar."""
        now = time.monotonic()
        live = {k: v for k, v in self.timers.items() if v[0] > now}
        self.timers = live
        if not live:
            self.engine.release("timer")
            return
        deadline, total = min(live.values())
        self.engine.claim("timer", "progress", self.accent, brightness=0.5,
                          value=(deadline - now) / total, restart=False)

    def _muted(self, muted: bool) -> None:
        if muted:
            self.engine.claim("muted", "ends", RED, brightness=0.5)
        else:
            self.engine.release("muted")

    def apply_user_light(self, data: dict[str, Any]) -> None:
        """Default handler: HA light_command -> 'user' claim."""
        if not data.get("state"):
            self.engine.release("user")
            return
        color = (float(data.get("red", 1.0)), float(data.get("green", 1.0)), float(data.get("blue", 1.0)))
        effect = USER_EFFECT_MAP.get(str(data.get("effect") or "None"), "solid")
        self.engine.claim("user", effect, color, brightness=float(data.get("brightness", 1.0)))


async def run_client(url: str, bridge: VoiceBridge, outbox: asyncio.Queue | None = None) -> None:
    """Connect forever. ``outbox`` carries commands (e.g. from a button) to LVA."""
    import websockets  # imported lazily so unit tests need no network deps

    started = time.monotonic()
    while True:
        try:
            async with websockets.connect(url, open_timeout=2, ping_interval=10) as ws:
                _LOG.info("connected to LVA at %s", url)
                bridge.engine.release("network:lva")
                for reg in bridge.registrations():
                    await ws.send(json.dumps(reg))
                sender = asyncio.create_task(_pump(ws, outbox)) if outbox else None
                try:
                    async for raw in ws:
                        try:
                            bridge.handle(json.loads(raw))
                        except (ValueError, TypeError):
                            _LOG.warning("bad message from LVA: %r", raw[:200])
                finally:
                    if sender:
                        sender.cancel()
        except Exception as exc:  # OSError, timeouts and websockets' own hierarchy
            _LOG.debug("LVA unavailable: %s", exc)
        # Retry fast inside LVA's startup window, then back off; flag after 15 s.
        since = time.monotonic() - started
        if since > 15:
            bridge.engine.claim("network:lva", "breathe", AMBER, brightness=0.3, priority=55)
        await asyncio.sleep(0.25 if since < 30 else 3.0)


async def _pump(ws: Any, outbox: asyncio.Queue) -> None:
    while True:
        cmd = await outbox.get()
        await ws.send(json.dumps(cmd))
