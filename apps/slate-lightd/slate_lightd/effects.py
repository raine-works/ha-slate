"""Stateless effect renderers for a linear edge bar.

Every effect is a pure function ``(t, n, color, value) -> Frame`` where ``t`` is
seconds since the effect started, ``n`` the LED count, ``color`` the base colour
and ``value`` an optional 0..1 parameter (progress fraction, etc.). Pure
functions keep effects trivially unit-testable and make it impossible for an
animation to leak state between voice-pipeline phases.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .color import BLACK, RGB, Frame, clamp, scale

Effect = Callable[[float, int, RGB, float], Frame]


def off(t: float, n: int, color: RGB, value: float) -> Frame:
    return [BLACK] * n


def solid(t: float, n: int, color: RGB, value: float) -> Frame:
    return [color] * n


def breathe(t: float, n: int, color: RGB, value: float, period: float = 3.0) -> Frame:
    k = 0.15 + 0.85 * (0.5 - 0.5 * math.cos(2 * math.pi * t / period))
    return [scale(color, k)] * n


def pulse(t: float, n: int, color: RGB, value: float) -> Frame:
    """Faster, deeper breathe used while the assistant is speaking."""
    return breathe(t, n, color, value, period=0.9)


def grow(t: float, n: int, color: RGB, value: float, duration: float = 0.35) -> Frame:
    """Fill from the centre outwards, then hold. Used on wake-word detection."""
    frac = clamp(t / duration)
    half = (n - 1) / 2.0
    return [color if abs(i - half) <= frac * (half + 0.5) else BLACK for i in range(n)]


def scan(t: float, n: int, color: RGB, value: float, period: float = 1.4, width: float = 0.18) -> Frame:
    """Soft comet sweeping back and forth. Used while Home Assistant is thinking."""
    if n == 1:
        return breathe(t, n, color, value)
    phase = (t % period) / period
    pos = (1 - abs(2 * phase - 1)) * (n - 1)  # triangle wave 0..n-1..0
    sigma = max(width * n, 1.0)
    return [scale(color, math.exp(-(((i - pos) / sigma) ** 2))) for i in range(n)]


def progress(t: float, n: int, color: RGB, value: float) -> Frame:
    """Fraction of the bar lit, with an anti-aliased leading edge (timers, OTA)."""
    lit = clamp(value) * n
    return [scale(color, clamp(lit - i)) for i in range(n)]


def flash(t: float, n: int, color: RGB, value: float, count: int = 3, period: float = 0.4) -> Frame:
    """``count`` hard flashes then dark (pipeline error)."""
    if t >= count * period:
        return [BLACK] * n
    return [color if (t % period) < period / 2 else BLACK] * n


def ends(t: float, n: int, color: RGB, value: float) -> Frame:
    """Only the two outermost LEDs lit: a quiet persistent indicator (mic muted)."""
    if n <= 2:
        return [color] * n
    return [color] + [BLACK] * (n - 2) + [color]


EFFECTS: dict[str, Effect] = {
    "off": off,
    "solid": solid,
    "breathe": breathe,
    "pulse": pulse,
    "grow": grow,
    "scan": scan,
    "progress": progress,
    "flash": flash,
    "ends": ends,
}

# Effects offered to Home Assistant on the user-controlled light entity.
USER_EFFECTS = ["None", "Breathe", "Scan", "Pulse"]
USER_EFFECT_MAP = {"None": "solid", "Breathe": "breathe", "Scan": "scan", "Pulse": "pulse"}
