"""Slate Link protocol v1 (device <-> Home Assistant integration).

Transport: WebSocket over TLS, ``wss://<device>:6054/v1``. The device presents a
self-signed certificate; the integration pins its SHA-256 fingerprint at pairing
time (trust on first use), so no CA is involved.

Text frames are JSON objects with a ``type`` field. A message carrying an ``id``
is a request and is answered by ``{"type": "result", "id": n, "ok": bool, ...}``.

Binary frames are ``<1-byte channel><payload>``:

    0x01 MIC    device -> HA   s16le 16 kHz mono capture for the Assist pipeline
    0x02 SPK    HA -> device   s16le PCM, format given by the preceding audio-start
    0x03 FILE   HA -> device   wake-word model upload chunk

Session start::

    device: {"type": "hello", "protocol": 1, "device_id", "name", "model",
             "sw_version", "paired": bool, "nonce": hex}
    HA:     {"type": "auth", "proof": hex(HMAC-SHA256(token, nonce))}
       or   {"type": "pair", "code": "123456" | null, "client": "Home Assistant"}
    device: {"type": "auth-ok", "token"?: str}  then  {"type": "state", ...}
       or   {"type": "auth-failed", "reason": str}  and close
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_PORT = 6054
ZEROCONF_TYPE = "_ha-slate._tcp.local."

CH_MIC = 0x01
CH_SPK = 0x02
CH_FILE = 0x03

MIC_RATE = 16000
MIC_WIDTH = 2
MIC_CHANNELS = 1


def proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode(), bytes.fromhex(nonce), hashlib.sha256).hexdigest()


def verify_proof(token: str, nonce: str, candidate: str) -> bool:
    return hmac.compare_digest(proof(token, nonce), str(candidate))


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def frame(channel: int, payload: bytes) -> bytes:
    return bytes([channel]) + payload
