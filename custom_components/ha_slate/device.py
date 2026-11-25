"""Runtime object for one panel: connection loop, cached state, wake word sync."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import wake_words
from .client import SlateAuthError, SlateClient, SlateConnectionError, SlateError
from .const import CH_FILE, CONF_DEVICE_ID, CONF_FINGERPRINT, CONF_TOKEN, UPLOAD_CHUNK_BYTES

if TYPE_CHECKING:
    from .assist_satellite import SlateAssistSatellite

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0


class SlateDevice:
    """Owns the client and fans device pushes out to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.state: dict[str, Any] = {}
        self.hello: dict[str, Any] = {}
        self.satellite: SlateAssistSatellite | None = None
        self.client: SlateClient | None = None
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._disconnected = asyncio.Event()
        self._stopping = False

    # -- lifecycle -------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    async def async_connect_first(self) -> None:
        """Initial connect during setup (raises so HA can retry / reauth)."""
        await self._connect()
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), f"ha_slate {self.device_id} connection")

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
        if self.client:
            await self.client.close()

    async def _connect(self) -> None:
        self._disconnected.clear()
        client = SlateClient(
            async_get_clientsession(self.hass),
            self.entry.data[CONF_HOST], self.entry.data[CONF_PORT], self.entry.data[CONF_FINGERPRINT],
            on_message=self._on_message, on_binary=self._on_binary, on_disconnect=self._on_disconnect,
            heartbeat=20,  # detect a dead panel (PoE unplugged) within ~40 s
        )
        await client.connect(self.entry.data[CONF_TOKEN])
        self.client = client
        self.hello = client.hello
        _LOGGER.debug("Connected to %s (%s)", self.hello.get("name"), self.entry.data[CONF_HOST])
        await self.async_sync_wake_words()
        self._notify()

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while not self._stopping:
            await self._disconnected.wait()
            if self._stopping:
                return
            self._notify()
            await asyncio.sleep(delay)
            try:
                await self._connect()
                delay = RECONNECT_MIN
            except SlateAuthError as err:
                _LOGGER.warning("%s rejected our credentials (%s); re-pair required", self.device_id, err.reason)
                self.entry.async_start_reauth(self.hass)
                return
            except SlateError as err:
                _LOGGER.debug("Reconnect to %s failed: %s", self.device_id, err)
                self._disconnected.set()
                delay = min(delay * 2, RECONNECT_MAX)

    # -- pushes ----------------------------------------------------------------
    @callback
    def _on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "state":
            self.state = msg
            self._notify()
            if self.satellite:
                self.satellite.async_device_state_changed()
        elif self.satellite is not None:
            self.satellite.async_handle_message(msg)

    @callback
    def _on_binary(self, channel: int, payload: bytes) -> None:
        if self.satellite is not None:
            self.satellite.async_handle_binary(channel, payload)

    @callback
    def _on_disconnect(self) -> None:
        if self.satellite:
            self.satellite.async_device_disconnected()
        self._disconnected.set()
        self._notify()

    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # -- commands --------------------------------------------------------------
    def _require(self) -> SlateClient:
        if not self.connected or self.client is None:
            raise SlateConnectionError("panel is not connected")
        return self.client

    @property
    def settings(self) -> dict[str, Any]:
        return self.state.get("settings", {})

    async def async_set_settings(self, **settings: Any) -> None:
        await self._require().request({"type": "set-config", "settings": settings})

    async def async_set_light(self, target: str, **state: Any) -> None:
        await self._require().request({"type": "light", "target": target, **state})

    async def async_command(self, msg_type: str, **data: Any) -> dict[str, Any]:
        return await self._require().request({"type": msg_type, **data})

    async def async_send(self, msg: dict[str, Any]) -> None:
        await self._require().send(msg)

    async def async_send_binary(self, channel: int, payload: bytes) -> None:
        await self._require().send_binary(channel, payload)

    async def async_sync_wake_words(self) -> dict[str, list[str]]:
        """Make the panel's custom models exactly match HA's custom_wake_words folder."""
        client = self._require()
        library = await wake_words.async_scan(self.hass)
        result = await client.request({"type": "wake-word-sync",
                                       "models": [{"id": w.id, "sha256": w.sha256} for w in library.values()]})
        uploaded: list[str] = []
        for wid in result.get("missing", []):
            ww = library[wid]
            data = await self.hass.async_add_executor_job(ww.model_path.read_bytes)
            await client.request({"type": "wake-word-upload", "wake_word_id": ww.id, "sha256": ww.sha256,
                                  "size": len(data), "config": ww.config})
            for i in range(0, len(data), UPLOAD_CHUNK_BYTES):
                await client.send_binary(CH_FILE, data[i:i + UPLOAD_CHUNK_BYTES])
            await client.request({"type": "wake-word-upload-end", "wake_word_id": ww.id}, timeout=60)
            uploaded.append(wid)
        if uploaded or result.get("removed"):
            _LOGGER.info("Wake words synced to %s: uploaded %s, removed %s",
                         self.device_id, uploaded, result.get("removed", []))
        return {"uploaded": uploaded, "removed": list(result.get("removed", []))}
