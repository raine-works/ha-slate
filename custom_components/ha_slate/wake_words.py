"""Custom wake word library: ``<config>/custom_wake_words/<id>.json + <id>.tflite``.

This is the folder Home Assistant's ESPHome integration already reads, so a
model dropped there once is offered to every voice satellite in the house.
HA Slate additionally accepts openWakeWord models (``"type": "openWakeWord"``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import WAKE_WORDS_DIR_NAME

_LOGGER = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
SUPPORTED_TYPES = {"micro", "microwakeword", "openwakeword", "oww"}
MAX_MODEL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CustomWakeWord:
    id: str
    wake_word: str
    config: dict[str, Any]
    model_path: Path
    sha256: str
    size: int


def scan(config_dir: Path) -> dict[str, CustomWakeWord]:
    """Blocking: read and validate every model in the folder."""
    found: dict[str, CustomWakeWord] = {}
    if not config_dir.is_dir():
        return found
    for cfg_path in sorted(config_dir.glob("*.json")):
        wid = cfg_path.stem
        model = cfg_path.with_suffix(".tflite")
        try:
            if not ID_RE.match(wid):
                raise ValueError("file name must be lowercase letters, digits, - or _")
            if not model.is_file():
                raise ValueError(f"missing {model.name}")
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
            if str(config.get("type", "")).lower() not in SUPPORTED_TYPES:
                raise ValueError(f"unsupported type {config.get('type')!r}")
            if not str(config.get("wake_word", "")).strip():
                raise ValueError("wake_word is required")
            data = model.read_bytes()
            if len(data) > MAX_MODEL_BYTES or data[4:8] != b"TFL3":
                raise ValueError("not a TensorFlow Lite model (or larger than 8 MB)")
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring custom wake word %s: %s", cfg_path.name, err)
            continue
        found[wid] = CustomWakeWord(wid, config["wake_word"], config, model,
                                    hashlib.sha256(data).hexdigest(), len(data))
    return found


async def async_scan(hass: HomeAssistant) -> dict[str, CustomWakeWord]:
    return await hass.async_add_executor_job(scan, Path(hass.config.path(WAKE_WORDS_DIR_NAME)))
