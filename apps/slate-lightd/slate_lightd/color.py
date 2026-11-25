"""Colour helpers: frames are lists of (r, g, b) float tuples in 0.0..1.0."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

RGB = tuple[float, float, float]
Frame = list[RGB]

BLACK: RGB = (0.0, 0.0, 0.0)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Limit ``x`` to ``lo..hi``."""
    return lo if x < lo else hi if x > hi else x


def scale(c: RGB, k: float) -> RGB:
    """Multiply each channel of ``c`` by ``k``; the result isn't clamped."""
    return (c[0] * k, c[1] * k, c[2] * k)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """Blend linearly from ``a`` (``t`` = 0) to ``b`` (``t`` = 1); ``t`` is clamped to 0..1."""
    t = clamp(t)
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def from_bytes(rgb: Sequence[int]) -> RGB:
    """[0..255]*3 -> RGB floats."""
    return (clamp(rgb[0] / 255.0), clamp(rgb[1] / 255.0), clamp(rgb[2] / 255.0))


def gamma_lut(gamma: float = 2.2, size: int = 256) -> list[int]:
    """Perceptual correction table: linear 0..1 input index -> 8-bit PWM duty."""
    return [round(math.pow(i / (size - 1), gamma) * 255) for i in range(size)]


def to_bytes(frame: Iterable[RGB], lut: Sequence[int]) -> list[tuple[int, int, int]]:
    """Turn a float frame into the 8-bit frame a backend writes, through ``lut`` (``gamma_lut``).

    Channels are clamped to 0..1 first, so an over-bright effect saturates instead of failing.
    """
    top = len(lut) - 1
    return [
        (lut[round(clamp(r) * top)], lut[round(clamp(g) * top)], lut[round(clamp(b) * top)])
        for r, g, b in frame
    ]
