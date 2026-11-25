"""Backends without hardware: one for tests, one that previews the bar in a terminal."""

from __future__ import annotations

import sys

from .base import Backend


class NullBackend(Backend):
    """Discards output; keeps the last frame for tests."""

    def __init__(self, led_count: int) -> None:
        self.led_count = led_count
        self.last: list[tuple[int, int, int]] | None = None

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        """Keep ``frame`` as ``last`` and output nothing."""
        self.last = frame


class ConsoleBackend(NullBackend):
    """Truecolor terminal preview, for running on a dev host or in QEMU."""

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        """Redraw the bar as one line of coloured cells, only when the frame changed."""
        if frame == self.last:
            return
        self.last = frame
        cells = "".join(f"\x1b[48;2;{r};{g};{b}m \x1b[0m" for r, g, b in frame)
        sys.stdout.write(f"\r{cells}")
        sys.stdout.flush()
