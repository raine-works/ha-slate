"""Config flow against a real panel agent."""

from ipaddress import ip_address
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.ha_slate.const import CONF_DEVICE_ID, CONF_FINGERPRINT, CONF_TOKEN, DOMAIN

from .conftest import AgentRig


@pytest.fixture(autouse=True)
def no_entry_setup():
    """Flow tests stop at entry creation; setup is covered in test_satellite."""
    with patch("custom_components.ha_slate.async_setup_entry", return_value=True), \
         patch("custom_components.ha_slate.async_unload_entry", return_value=True):
        yield


async def test_user_flow_pairs(hass: HomeAssistant, rig) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: rig.port})
    assert result["step_id"] == "pair"
    assert result["description_placeholders"]["name"] == "Kitchen Slate"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen Slate"
    data = result["data"]
    assert data[CONF_DEVICE_ID] == "02aabbccddee"
    assert data[CONF_FINGERPRINT] == rig.fingerprint
    assert data[CONF_TOKEN] == rig.agent.pairing.token
    assert result["result"].unique_id == "02aabbccddee"


async def test_user_flow_cannot_connect(hass: HomeAssistant, rig) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_pairing_code_required(hass: HomeAssistant, socket_enabled, tmp_path) -> None:
    rig = AgentRig(tmp_path / "coded", pairing_code="482913")
    await rig.start()
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: rig.port})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "invalid_code"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pairing_code": "482913"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
    finally:
        await rig.stop()


async def test_zeroconf_flow(hass: HomeAssistant, rig) -> None:
    info = ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")], port=rig.port,
        hostname="ha-slate-02aabbccddee.local.", type="_ha-slate._tcp.local.",
        name="ha-slate-02aabbccddee._ha-slate._tcp.local.",
        properties={"id": "02aabbccddee", "name": "Kitchen Slate", "protocol": "1"},
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
                                                       data=info)
    assert result["step_id"] == "zeroconf_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Rediscovery of a configured panel aborts (and would update its address).
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
                                                      data=info)
    assert again["type"] is FlowResultType.ABORT and again["reason"] == "already_configured"
