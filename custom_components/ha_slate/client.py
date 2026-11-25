"""Slate Link client: TLS WebSocket to the panel's slate-agent.

The panel presents a self-signed certificate. Its SHA-256 fingerprint is
captured at pairing time (trust on first use) and pinned on every connection
afterwards, so a device swap or man-in-the-middle fails closed and triggers a
re-authentication flow instead of silently connecting.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import itertools
import json
import logging
import ssl
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import PROTOCOL_VERSION

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
REQUEST_TIMEOUT = 15.0


class SlateError(Exception):
    """Base error."""


class SlateConnectionError(SlateError):
    """Device unreachable, TLS fingerprint mismatch or protocol error."""


class SlateAuthError(SlateError):
    """Token rejected or pairing refused. ``reason`` is the device's code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def async_fetch_fingerprint(host: str, port: int) -> str:
    """Return the SHA-256 fingerprint of the device certificate (no verification)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            _, writer = await asyncio.open_connection(host, port, ssl=ctx)
    except (OSError, TimeoutError) as err:
        raise SlateConnectionError(str(err)) from err
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return hashlib.sha256(der).hexdigest()


class SlateClient:
    """One authenticated session. Owner supplies callbacks for pushes."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        fingerprint: str,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_binary: Callable[[int, bytes], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        heartbeat: float | None = None,
    ) -> None:
        self._session = session
        self._heartbeat = heartbeat
        self.host = host
        self.port = port
        self.fingerprint = fingerprint
        self.on_message = on_message
        self.on_binary = on_binary
        self.on_disconnect = on_disconnect
        self.hello: dict[str, Any] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self, token: str | None = None, *, pair: bool = False,
                      pairing_code: str | None = None) -> dict[str, Any]:
        """Open, authenticate (or pair). Returns the auth-ok message (with token if paired)."""
        url = f"wss://{self.host}:{self.port}/v1"
        try:
            self._ws = await self._session.ws_connect(
                url,
                ssl=aiohttp.Fingerprint(bytes.fromhex(self.fingerprint)),
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
                heartbeat=self._heartbeat,
                max_msg_size=4 * 1024 * 1024,
            )
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self.hello = await self._receive_json()
                if self.hello.get("type") != "hello" or self.hello.get("protocol") != PROTOCOL_VERSION:
                    raise SlateConnectionError(f"unsupported device protocol: {self.hello}")
                if pair:
                    await self._send({"type": "pair", "code": pairing_code, "client": "Home Assistant"})
                else:
                    if token is None:
                        raise SlateAuthError("not_paired")
                    proof = hmac.new(token.encode(), bytes.fromhex(self.hello["nonce"]), hashlib.sha256).hexdigest()
                    await self._send({"type": "auth", "proof": proof})
                result = await self._receive_json()
        except aiohttp.ServerFingerprintMismatch as err:
            await self.close()
            raise SlateAuthError("fingerprint_mismatch") from err
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
            await self.close()
            raise SlateConnectionError(str(err) or type(err).__name__) from err
        except SlateError:
            await self.close()
            raise
        if result.get("type") != "auth-ok":
            await self.close()
            raise SlateAuthError(str(result.get("reason", "auth_failed")))
        self._reader = asyncio.create_task(self._read_loop(), name=f"ha_slate reader {self.host}")
        return result

    async def _receive_json(self) -> dict[str, Any]:
        assert self._ws is not None
        msg = await self._ws.receive()
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise SlateConnectionError(f"unexpected frame during handshake: {msg.type}")
        return json.loads(msg.data)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "result" and (fut := self._pending.pop(data.get("id"), None)):
                        if not fut.done():
                            fut.set_result(data)
                    elif self.on_message:
                        self.on_message(data)
                elif msg.type is aiohttp.WSMsgType.BINARY and msg.data and self.on_binary:
                    self.on_binary(msg.data[0], msg.data[1:])
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("%s: read loop ended: %s", self.host, err)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(SlateConnectionError("disconnected"))
            self._pending.clear()
            self._ws = None
            if self.on_disconnect:
                self.on_disconnect()

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise SlateConnectionError("not connected")
        async with self._send_lock:
            await self._ws.send_str(json.dumps(msg, separators=(",", ":")))

    async def send(self, msg: dict[str, Any]) -> None:
        """Fire-and-forget message."""
        try:
            await self._send(msg)
        except (aiohttp.ClientError, ConnectionError) as err:
            raise SlateConnectionError(str(err)) from err

    async def send_binary(self, channel: int, payload: bytes) -> None:
        if self._ws is None or self._ws.closed:
            raise SlateConnectionError("not connected")
        async with self._send_lock:
            await self._ws.send_bytes(bytes([channel]) + payload)

    async def request(self, msg: dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send({**msg, "id": rid})
            async with asyncio.timeout(timeout):
                result = await fut
        except TimeoutError as err:
            raise SlateConnectionError(f"timeout waiting for {msg.get('type')}") from err
        finally:
            self._pending.pop(rid, None)
        if not result.get("ok"):
            raise SlateError(str(result.get("error", "request failed")))
        return result

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._reader, 2)
            self._reader = None
