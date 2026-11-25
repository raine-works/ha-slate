"""The interface every light bar output backend implements."""

from __future__ import annotations

import abc


class Backend(abc.ABC):
    """An output for the light bar: takes finished 8-bit frames and puts them on the hardware.

    Frames arrive already gamma-corrected and power-limited, one ``(r, g, b)`` tuple per LED,
    ``led_count`` long. A backend owns its device handle and releases it in ``close``.
    """

    led_count: int

    @abc.abstractmethod
    def write(self, frame: list[tuple[int, int, int]]) -> None:
        """Push one frame. Must not block for longer than one frame period."""

    def close(self) -> None:  # pragma: no cover - trivial
        """Release the device. The default does nothing; backends holding a handle override it."""
