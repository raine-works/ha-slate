from __future__ import annotations

import sys

from .base import Backend


class NullBackend(Backend):
    """Discards output; keeps the last frame for tests."""

    def __init__(self, led_count: int) -> None:
        self.led_count = led_count
        self.last: list[tuple[int, int, int]] | None = None

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        self.last = frame


class ConsoleBackend(NullBackend):
    """Truecolor terminal preview, for running on a dev host or in QEMU."""

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        if frame == self.last:
            return
        self.last = frame
        cells = "".join(f"\x1b[48;2;{r};{g};{b}m \x1b[0m" for r, g, b in frame)
        sys.stdout.write(f"\r{cells}")
        sys.stdout.flush()
