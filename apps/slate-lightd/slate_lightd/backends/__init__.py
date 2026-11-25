"""Output backends. Each takes post-gamma, post-limit 8-bit frames."""

from __future__ import annotations

from typing import Any

from .base import Backend
from .null import ConsoleBackend, NullBackend
from .sysfs import SysfsLedBackend
from .ws2812_spi import Ws2812SpiBackend


def create(name: str, led_count: int, opts: dict[str, Any]) -> Backend:
    """Build the backend named in the config.

    ``name`` is ``ws2812-spi``, ``sysfs``, ``console`` or ``null``; ``opts`` holds that backend's
    settings (``sysfs`` requires ``leds``; the others fall back to defaults). Opening the device
    can raise ``OSError``. Raises ``ValueError`` for an unknown name and ``KeyError`` when
    ``sysfs`` has no ``leds``.
    """
    if name == "ws2812-spi":
        return Ws2812SpiBackend(
            led_count,
            device=opts.get("device", "/dev/spidev0.0"),
            speed_hz=int(opts.get("speed_hz", 3_200_000)),
            color_order=opts.get("color_order", "GRB"),
        )
    if name == "sysfs":
        return SysfsLedBackend(opts["leds"], root=opts.get("root", "/sys/class/leds"))
    if name == "console":
        return ConsoleBackend(led_count)
    if name == "null":
        return NullBackend(led_count)
    raise ValueError(f"unknown backend {name!r}")


__all__ = ["Backend", "create"]
