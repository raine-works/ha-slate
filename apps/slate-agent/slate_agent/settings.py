"""Persistent user settings (owned by Home Assistant, cached on the device)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

SENSITIVITIES = ("default", "more_sensitive", "less_sensitive")
WAKE_LOCATIONS = ("device", "home_assistant")


@dataclass
class Settings:
    """The panel's user settings; Home Assistant sets them, the agent persists them."""

    active_wake_words: list[str] = field(default_factory=lambda: ["okay_nabu"])
    wake_word_sensitivity: str = "default"
    # device: on-device microWakeWord/openWakeWord (default, private, low latency)
    # home_assistant: stream audio continuously to the pipeline's wake word engine
    wake_word_location: str = "device"
    muted: bool = False
    volume: float = 0.8
    wake_sound: bool = True
    kiosk_url: str | None = None

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply a partial update; return what actually changed.

        Raises ``ValueError`` for an unknown key or an invalid value; keys before
        the bad one have already been applied. Raises ``TypeError`` for a value of
        the wrong type (a non-number volume, a non-iterable wake word list). Volume
        is clamped to 0–1.
        """
        known = {f.name for f in fields(self)}
        applied: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in known:
                raise ValueError(f"unknown setting {key!r}")
            if key == "active_wake_words":
                value = [str(v) for v in value]
            elif key == "wake_word_sensitivity" and value not in SENSITIVITIES:
                raise ValueError(f"wake_word_sensitivity must be one of {SENSITIVITIES}")
            elif key == "wake_word_location" and value not in WAKE_LOCATIONS:
                raise ValueError(f"wake_word_location must be one of {WAKE_LOCATIONS}")
            elif key == "volume":
                value = max(0.0, min(1.0, float(value)))
            elif key in ("muted", "wake_sound"):
                value = bool(value)
            elif key == "kiosk_url" and value is not None:
                value = str(value)
                if not value.startswith(("http://", "https://")):
                    raise ValueError("kiosk_url must be http(s)")
            if getattr(self, key) != value:
                setattr(self, key, value)
                applied[key] = value
        return applied

    def as_dict(self) -> dict[str, Any]:
        """The settings as a JSON-ready dict."""
        return asdict(self)


class SettingsStore:
    """Settings persisted as JSON at one path (under ``/data`` on the panel)."""

    def __init__(self, path: Path) -> None:
        """Use the file at ``path``; it needn't exist yet."""
        self.path = path

    def load(self) -> Settings:
        """Read the saved settings, ignoring unknown keys.

        A missing file gives the defaults; one that can't be read, isn't JSON, or
        holds an invalid value is logged, keeping the settings applied before the
        error. Raises ``AttributeError`` for JSON that isn't an object, and
        ``TypeError`` for a value of the wrong type (e.g. ``"volume": null``).
        """
        s = Settings()
        try:
            data = json.loads(self.path.read_text())
            s.update({k: v for k, v in data.items() if k in s.as_dict()})
        except (OSError, ValueError) as exc:
            if self.path.exists():
                _LOG.warning("ignoring unreadable settings %s: %s", self.path, exc)
        return s

    def save(self, s: Settings) -> None:
        """Write ``s`` atomically (temp file, then rename), creating the directory; raises ``OSError`` on failure."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s.as_dict(), indent=1))
        os.replace(tmp, self.path)
