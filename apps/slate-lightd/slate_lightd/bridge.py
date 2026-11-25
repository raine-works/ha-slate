"""Voice-state to light mapping.

slate-agent (the voice satellite) forwards pipeline events over lightd's
control socket as ``{"voice_event": {"event": ..., "data": {...}}}``; this
module turns them into engine claims. Event vocabulary:

    wake_word_detected, listening, thinking, tts_speaking, tts_finished, idle,
    pipeline_error, disconnected, zeroconf{status}, muted{muted},
    timer_ticking / timer_updated / timer_ringing{id,total_seconds,seconds_left},
    volume_changed{volume}
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .color import RGB
from .effects import USER_EFFECT_MAP
from .engine import Engine

_LOG = logging.getLogger(__name__)

RED: RGB = (1.0, 0.0, 0.0)
AMBER: RGB = (1.0, 0.45, 0.0)
WHITE: RGB = (1.0, 1.0, 1.0)


class VoiceBridge:
    """Maps slate-agent voice events onto engine claims."""

    def __init__(self, engine: Engine, accent: RGB) -> None:
        self.engine = engine
        self.accent = accent
        self.timers: dict[str, tuple[float, float]] = {}  # id -> (deadline, total)

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
        """Home Assistant "Edge Light" entity state -> 'user' claim."""
        if not data.get("state"):
            self.engine.release("user")
            return
        color = (float(data.get("red", 1.0)), float(data.get("green", 1.0)), float(data.get("blue", 1.0)))
        effect = USER_EFFECT_MAP.get(str(data.get("effect") or "None"), "solid")
        self.engine.claim("user", effect, color, brightness=float(data.get("brightness", 1.0)))
