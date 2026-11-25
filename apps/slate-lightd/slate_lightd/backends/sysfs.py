"""Kernel LED-class backend for I2C constant-current drivers.

Use this when the edge bar is built from RGB segments on a driver IC with a
mainline kernel driver (TI LP50xx -> leds-lp50xx, ISSI IS31FL32xx, NXP PCA963x,
...). The device tree names each segment; the kernel enforces current limits in
hardware, and the multicolor class exposes ``multi_intensity`` + ``brightness``.
Monochrome LEDs (``leds-pwm``, ``leds-gpio``) are driven by luminance.
"""

from __future__ import annotations

import os

from .base import Backend


class _Led:
    def __init__(self, path: str) -> None:
        self.path = path
        with open(os.path.join(path, "max_brightness")) as f:
            self.max = int(f.read())
        self.multicolor = os.path.exists(os.path.join(path, "multi_intensity"))
        if self.multicolor:
            with open(os.path.join(path, "multi_index")) as f:
                self.index = f.read().split()  # e.g. ["red", "green", "blue"]
        # One pwrite() per update: sysfs attributes must be written in a single
        # syscall at offset 0, so no buffered file objects here.
        self.brightness = os.open(os.path.join(path, "brightness"), os.O_WRONLY)
        self.intensity = os.open(os.path.join(path, "multi_intensity"), os.O_WRONLY) if self.multicolor else None
        self.last: tuple[int, int, int] | None = None

    def set(self, rgb: tuple[int, int, int]) -> None:
        if rgb == self.last:
            return  # sysfs writes are I2C transactions; skip no-ops
        self.last = rgb
        if self.intensity is not None:
            by_name = {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}
            vals = [by_name.get(ch, max(rgb)) for ch in self.index]
            os.pwrite(self.intensity, " ".join(str(round(v * self.max / 255)) for v in vals).encode(), 0)
            os.pwrite(self.brightness, str(self.max if any(rgb) else 0).encode(), 0)
        else:
            lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
            os.pwrite(self.brightness, str(round(lum * self.max / 255)).encode(), 0)


class SysfsLedBackend(Backend):
    def __init__(self, leds: list[str], root: str = "/sys/class/leds") -> None:
        self.leds = [_Led(os.path.join(root, name)) for name in leds]
        self.led_count = len(self.leds)

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        for led, rgb in zip(self.leds, frame):
            led.set(rgb)
