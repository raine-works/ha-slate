"""Client for slate-lightd's local control socket (best effort, never blocks voice)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

_LOG = logging.getLogger(__name__)
DEFAULT_SOCKET = "/run/slate-lightd/control.sock"


class LightdClient:
    """Sends JSON-line requests to slate-lightd; failures are logged and return None, never raised."""

    def __init__(self, path: str | None = DEFAULT_SOCKET) -> None:
        """Use the control socket at ``path``; None records requests in ``sent`` without sending them."""
        self.path = path
        self._lock = asyncio.Lock()
        self._rw: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self.sent: list[dict[str, Any]] = []  # last messages, for diagnostics/tests

    async def request(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Send ``msg`` and return lightd's reply.

        Each step times out after 1 s and a dropped connection is retried once;
        returns None if lightd is unreachable or replies with invalid JSON.
        """
        self.sent = (self.sent + [msg])[-50:]
        if not self.path:
            return None
        async with self._lock:
            for _ in range(2):  # one reconnect attempt
                try:
                    if self._rw is None:
                        self._rw = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 1.0)
                    reader, writer = self._rw
                    writer.write((json.dumps(msg) + "\n").encode())
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), 1.0)
                    if not line:
                        raise ConnectionError("lightd closed the socket")
                    return json.loads(line)
                except (TimeoutError, OSError, ConnectionError, ValueError) as exc:
                    _LOG.debug("lightd unavailable: %s", exc)
                    self._rw = None
        return None

    async def voice_event(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Tell lightd about a voice pipeline event so the bar can show it."""
        await self.request({"voice_event": {"event": event, "data": data or {}}})

    async def set_light(self, target: str, state: dict[str, Any]) -> dict[str, Any] | None:
        """Change light ``target`` (``edge`` or ``display``) to ``state``; returns lightd's reply, or None."""
        return await self.request({"light": {"target": target, **state}})

    async def lights(self) -> dict[str, Any]:
        """Every light's state by target, or an empty dict if lightd is unreachable."""
        resp = await self.request({"status": True}) or {}
        return resp.get("lights", {})
