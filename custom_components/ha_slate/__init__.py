"""HA Slate: PoE wall panel with an on-device voice satellite."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .client import SlateAuthError, SlateError
from .const import DOMAIN, SERVICE_SYNC_WAKE_WORDS
from .device import SlateDevice

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.ASSIST_SATELLITE,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SlateConfigEntry = ConfigEntry[SlateDevice]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _sync(call: ServiceCall) -> ServiceResponse:
        results = {}
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            try:
                results[entry.title] = await entry.runtime_data.async_sync_wake_words()
            except SlateError as err:
                raise HomeAssistantError(f"{entry.title}: {err}") from err
        return results

    hass.services.async_register(DOMAIN, SERVICE_SYNC_WAKE_WORDS, _sync,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry) -> bool:
    device = SlateDevice(hass, entry)
    try:
        await device.async_connect_first()
    except SlateAuthError as err:
        raise ConfigEntryAuthFailed(f"Panel rejected credentials: {err.reason}") from err
    except SlateError as err:
        raise ConfigEntryNotReady(f"Cannot connect to panel: {err}") from err
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SlateConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SlateConfigEntry) -> None:
    """Best effort: tell the panel to forget us so it can be paired again."""
    from homeassistant.const import CONF_HOST, CONF_PORT
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .client import SlateClient
    from .const import CONF_FINGERPRINT, CONF_TOKEN

    client = SlateClient(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT],
                         entry.data[CONF_FINGERPRINT])
    try:
        await client.connect(entry.data[CONF_TOKEN])
        await client.request({"type": "unpair"}, timeout=5)
    except SlateError as err:
        _LOGGER.debug("Could not unpair %s: %s", entry.title, err)
    finally:
        await client.close()
