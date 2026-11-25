"""Priority-claim render engine.

Several producers want the bar at once: the voice pipeline, Home Assistant's
user-controlled light entity, the OTA updater, network supervision. Rather than
a hand-written state machine, each producer holds a *claim* at a fixed priority
and the highest-priority live claim is rendered. Releasing a claim reveals the
one underneath, so "error flash during a timer countdown during user ambient
light" composes for free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .backends import Backend
from .color import RGB, gamma_lut, scale, to_bytes
from .effects import EFFECTS
from .power import PowerBudget

_LOG = logging.getLogger(__name__)

# Higher wins. Gaps leave room for product-specific sources.
PRIORITY = {
    "user": 10,  # HA light entity / ambient
    "muted": 20,  # persistent mic-muted indicator
    "timer": 30,  # countdown
    "media": 35,
    "voice": 40,  # wake / listen / think / speak
    "alarm": 50,  # timer ringing
    "network": 60,  # HA unreachable
    "error": 70,  # transient pipeline error
    "system": 90,  # boot, OTA, factory reset
}


@dataclass
class Claim:
    """One producer's request for the bar.

    ``source`` names the producer (``kind`` or ``kind:detail``; ``kind`` picks the
    default priority from ``PRIORITY``). ``color`` and ``brightness`` are 0–1,
    ``value`` is the effect's parameter (e.g. progress 0–1), and ``started`` is the
    monotonic time the effect's animation is measured from.
    """

    source: str
    effect: str = "solid"
    color: RGB = (1.0, 1.0, 1.0)
    brightness: float = 1.0
    value: float = 0.0
    ttl: float | None = None  # seconds; None = until released
    priority: int = 0
    started: float = field(default_factory=time.monotonic)

    def expired(self, now: float) -> bool:
        """Whether the claim's ``ttl`` has run out at monotonic time ``now`` (never, without a ttl)."""
        return self.ttl is not None and now - self.started >= self.ttl


class Engine:
    """Renders the highest-priority live claim to a backend, within the power budget."""

    def __init__(self, backend: Backend, budget: PowerBudget, fps: float = 50.0, gamma: float = 2.2, reverse: bool = False) -> None:
        """Drive ``backend`` at up to ``fps`` frames a second while animating.

        ``gamma`` sets the colour correction curve; ``reverse`` flips the bar's
        direction for panels whose first LED is at the other end.
        """
        self.backend = backend
        self.budget = budget
        self.period = 1.0 / fps
        self.lut = gamma_lut(gamma)
        self.reverse = reverse
        self.claims: dict[str, Claim] = {}
        self._wake = asyncio.Event()

    # -- producer API -------------------------------------------------------
    def claim(self, source: str, effect: str, color: RGB, *, brightness: float = 1.0, value: float = 0.0,
              ttl: float | None = None, priority: int | None = None, restart: bool = True) -> None:
        """Set (or replace) ``source``'s claim.

        ``priority`` defaults to ``PRIORITY`` for the source's kind (0 if unknown);
        ``ttl`` drops the claim after that many seconds. With ``restart=False`` and
        the same effect, the animation keeps its phase (for value updates).
        Raises ``ValueError`` for an effect not in ``EFFECTS``.
        """
        if effect not in EFFECTS:
            raise ValueError(f"unknown effect {effect!r}")
        prio = priority if priority is not None else PRIORITY.get(source.split(":")[0], 0)
        old = self.claims.get(source)
        c = Claim(source, effect, color, brightness, value, ttl, prio)
        if old is not None and not restart and old.effect == effect:
            c.started = old.started  # keep animation phase continuous on value updates
        self.claims[source] = c
        self._wake.set()

    def release(self, source: str) -> None:
        """Drop ``source``'s claim, revealing the one beneath; a no-op if it holds none."""
        if self.claims.pop(source, None) is not None:
            self._wake.set()

    def active(self) -> Claim | None:
        """Prune expired claims and return the highest-priority one left, or None."""
        now = time.monotonic()
        for k in [k for k, c in self.claims.items() if c.expired(now)]:
            del self.claims[k]
        return max(self.claims.values(), key=lambda c: c.priority, default=None)

    # -- rendering ----------------------------------------------------------
    def render(self, now: float | None = None) -> list[tuple[int, int, int]]:
        """Compute the frame for monotonic time ``now`` (default: the current time).

        Returns one gamma-corrected, power-limited 0–255 RGB tuple per LED; all
        off when no claim is live.
        """
        now = time.monotonic() if now is None else now
        n = self.backend.led_count
        top = self.active()
        if top is None:
            frame = EFFECTS["off"](0.0, n, (0.0, 0.0, 0.0), 0.0)
        else:
            frame = EFFECTS[top.effect](now - top.started, n, scale(top.color, top.brightness), top.value)
        if self.reverse:
            frame.reverse()
        return self.budget.limit(to_bytes(frame, self.lut))

    async def run(self) -> None:
        """Render forever, writing only changed frames; backend errors are logged, not raised.

        Animated effects render at ``fps``; static ones wait for a claim change
        (or a 1 s tick, so ttl expiry is noticed).
        """
        last: list[tuple[int, int, int]] | None = None
        while True:
            frame = self.render()
            if frame != last:
                try:
                    self.backend.write(frame)
                except OSError:
                    _LOG.exception("backend write failed")
                last = frame
            top = self.active()
            animated = top is not None and top.effect not in ("solid", "off", "ends", "progress")
            # Static frames: sleep until something changes (zero CPU when idle).
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.period if animated else 1.0)
            except TimeoutError:
                pass
