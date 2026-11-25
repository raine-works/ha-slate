"""The integration's checks on the panel: its pinned certificate and its protocol version."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_slate.client import SlateAuthError, SlateClient, SlateConnectionError
from custom_components.ha_slate.const import (
    CONF_DEVICE_ID,
    CONF_FINGERPRINT,
    CONF_TOKEN,
    DOMAIN,
    PROTOCOL_VERSION,
)

# Not the rig's certificate: the SHA-256 of nothing.
WRONG_FINGERPRINT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _entry(hass: HomeAssistant, rig, token: str, fingerprint: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="02aabbccddee", title="Kitchen Slate", data={
        CONF_HOST: "127.0.0.1", CONF_PORT: rig.port, CONF_FINGERPRINT: fingerprint,
        CONF_TOKEN: token, CONF_DEVICE_ID: "02aabbccddee"})
    entry.add_to_hass(hass)
    return entry


async def _wait(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def _reauth_started(hass: HomeAssistant) -> bool:
    return any(f["context"]["source"] == config_entries.SOURCE_REAUTH
               for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN))


def _track_dropped(rig) -> list:
    """Record each connection the panel's server lets go of.

    A client refusing the certificate hangs up after TLS, before the WebSocket handshake; the
    panel drops that connection in the background, and stopping the server before it has trips
    a websockets 12 bug at teardown. Tests wait for this list before they end.
    """
    dropped: list = []
    unregister = rig.server.unregister
    rig.server.unregister = lambda protocol: (dropped.append(protocol), unregister(protocol))
    return dropped


async def test_client_refuses_certificate_not_pinned(hass: HomeAssistant, rig) -> None:
    token, err = rig.agent.pairing.try_pair(None)
    assert token, err
    dropped = _track_dropped(rig)
    client = SlateClient(async_get_clientsession(hass), "127.0.0.1", rig.port, WRONG_FINGERPRINT)
    with pytest.raises(SlateAuthError) as exc:
        await client.connect(token)
    assert exc.value.reason == "fingerprint_mismatch"
    assert not client.connected
    assert rig.agent.session is None
    await _wait(lambda: dropped)


async def test_setup_with_wrong_fingerprint_starts_reauth(hass: HomeAssistant, rig) -> None:
    token, err = rig.agent.pairing.try_pair(None)
    assert token, err
    dropped = _track_dropped(rig)
    entry = _entry(hass, rig, token, WRONG_FINGERPRINT)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_started(hass)
    assert rig.agent.session is None
    await _wait(lambda: dropped)


# aiohttp reschedules the heartbeat when the panel's close reply arrives after close() has
# cancelled it (ClientWebSocketResponse._on_data_received doesn't check _closed), leaving a
# timer on the closed socket. The connection itself is closed, which the test checks.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_setup_refuses_other_protocol_version(hass: HomeAssistant, rig) -> None:
    token, err = rig.agent.pairing.try_pair(None)
    assert token, err
    entry = _entry(hass, rig, token, rig.fingerprint)
    with patch("slate_agent.agent.PROTOCOL_VERSION", PROTOCOL_VERSION + 1):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert rig.agent.session is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_pairing_refuses_other_protocol_version(hass: HomeAssistant, rig) -> None:
    with patch("slate_agent.agent.PROTOCOL_VERSION", PROTOCOL_VERSION + 1):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: rig.port})
    assert result["errors"] == {"base": "cannot_connect"}
    assert not rig.agent.pairing.paired


async def test_client_refuses_other_protocol_version(hass: HomeAssistant, rig) -> None:
    token, err = rig.agent.pairing.try_pair(None)
    assert token, err
    client = SlateClient(async_get_clientsession(hass), "127.0.0.1", rig.port, rig.fingerprint)
    with patch("slate_agent.agent.PROTOCOL_VERSION", PROTOCOL_VERSION + 1), \
         pytest.raises(SlateConnectionError, match="unsupported device protocol"):
        await client.connect(token)
    assert not client.connected
    assert rig.agent.session is None
