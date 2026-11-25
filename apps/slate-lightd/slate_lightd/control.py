"""Local control socket for system producers (boot, OTA, network supervisor).

Protocol: one JSON object per line on a Unix stream socket.

    {"claim": "system", "effect": "progress", "color": [0, 80, 255], "value": 0.42}
    {"release": "system"}
    {"status": true}        -> replies {"active": {...} | null, "claims": [...]}

The socket is created 0660 under /run/slate-lightd (root-owned), so only root
services (RAUC hooks, system units) can drive the bar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .color import from_bytes
from .engine import Engine

_LOG = logging.getLogger(__name__)


def apply(engine: Engine, req: dict[str, Any]) -> dict[str, Any]:
    if "release" in req:
        engine.release(str(req["release"]))
        return {"ok": True}
    if "claim" in req:
        engine.claim(
            str(req["claim"]),
            str(req.get("effect", "solid")),
            from_bytes(req.get("color", [255, 255, 255])),
            brightness=float(req.get("brightness", 1.0)),
            value=float(req.get("value", 0.0)),
            ttl=req.get("ttl"),
            priority=req.get("priority"),
            restart=bool(req.get("restart", False)),
        )
        return {"ok": True}
    if req.get("status"):
        top = engine.active()
        return {
            "active": None if top is None else {"source": top.source, "effect": top.effect, "priority": top.priority},
            "claims": sorted(engine.claims),
        }
    return {"ok": False, "error": "expected claim, release or status"}


async def serve(engine: Engine, path: str) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    resp = apply(engine, json.loads(line))
                except (ValueError, TypeError, KeyError) as exc:
                    resp = {"ok": False, "error": str(exc)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(handler, path)
    os.chmod(path, 0o660)
    _LOG.info("control socket at %s", path)
    return server
