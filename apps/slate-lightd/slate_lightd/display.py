"""Display backlight exposed to Home Assistant as a dimmable light."""

from __future__ import annotations

import logging
import os
from typing import Any

_LOG = logging.getLogger(__name__)


def find_backlight(name: str, root: str = "/sys/class/backlight") -> str | None:
    if name and name != "auto":
        path = os.path.join(root, name)
        return path if os.path.isdir(path) else None
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return None
    return os.path.join(root, entries[0]) if entries else None


class Backlight:
    def __init__(self, path: str, min_level: float = 0.05) -> None:
        self.path = path
        self.min_level = min_level
        with open(os.path.join(path, "max_brightness")) as f:
            self.max = int(f.read())

    def set(self, on: bool, level: float) -> None:
        # Perceptual curve: backlight PWM is linear in light output, eyes are not.
        level = max(self.min_level, min(1.0, level)) if on else 0.0
        raw = round((level ** 2.2) * self.max)
        if on and raw == 0:
            raw = 1
        with open(os.path.join(self.path, "brightness"), "w") as f:
            f.write(str(raw))

    def apply(self, data: dict[str, Any]) -> None:
        self.set(bool(data.get("state")), float(data.get("brightness", 1.0)))
