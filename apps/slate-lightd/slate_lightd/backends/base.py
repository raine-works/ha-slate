from __future__ import annotations

import abc


class Backend(abc.ABC):
    led_count: int

    @abc.abstractmethod
    def write(self, frame: list[tuple[int, int, int]]) -> None:
        """Push one frame. Must not block for longer than one frame period."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass
