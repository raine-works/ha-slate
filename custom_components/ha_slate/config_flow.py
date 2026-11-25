"""Config flow: discover (zeroconf) or enter a host, then pair.

Pairing captures the panel's TLS fingerprint (trust on first use) and a
long-term token. A factory pairing code (label on the device) is required if
the panel has one provisioned; otherwise the panel accepts pairing during the
first 15 minutes after power-on while it is unpaired.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .client import SlateAuthError, SlateClient, SlateConnectionError, async_fetch_fingerprint
from .const import (
    CONF_DEVICE_ID, CONF_FINGERPRINT, CONF_PAIRING_CODE, CONF_TOKEN, DEFAULT_PORT, DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PAIR_ERRORS = {
    "invalid_code": "invalid_code",
    "locked": "locked",
    "pairing_window_closed": "pairing_window_closed",
    "already_paired": "already_paired",
}


class SlateConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._fingerprint: str | None = None
        self._hello: dict[str, Any] = {}

    async def _probe(self) -> None:
        """Fetch the certificate fingerprint and the device's hello."""
        assert self._host
        self._fingerprint = await async_fetch_fingerprint(self._host, self._port)
        client = SlateClient(async_get_clientsession(self.hass), self._host, self._port, self._fingerprint)
        try:
            await client.connect(token=None)
        except SlateAuthError:
            pass  # expected: we only wanted the hello
        finally:
            self._hello = client.hello
            await client.close()
        if not self._hello.get("device_id"):
            raise SlateConnectionError("no hello from device")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host, self._port = user_input[CONF_HOST], user_input[CONF_PORT]
            try:
                await self._probe()
            except SlateConnectionError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(self._hello["device_id"])
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
                return await self.async_step_pair()
        schema = probatio.Schema({
            probatio.Required(CONF_HOST, default=self._host or ""): str,
            probatio.Required(CONF_PORT, default=self._port): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        device_id = discovery_info.properties.get("id")
        if not device_id:
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.host,
                                                     CONF_PORT: discovery_info.port or DEFAULT_PORT})
        self._host, self._port = discovery_info.host, discovery_info.port or DEFAULT_PORT
        name = discovery_info.properties.get("name") or f"HA Slate {device_id[-6:]}"
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="zeroconf_confirm",
                                        description_placeholders=self.context["title_placeholders"])
        try:
            await self._probe()
        except SlateConnectionError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_pair()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._host, self._port = entry_data[CONF_HOST], entry_data[CONF_PORT]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        try:
            await self._probe()
        except SlateConnectionError:
            return self.async_abort(reason="cannot_connect")
        if self._hello["device_id"] != self._get_reauth_entry().data[CONF_DEVICE_ID]:
            return self.async_abort(reason="wrong_device")
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._host and self._fingerprint
            client = SlateClient(async_get_clientsession(self.hass), self._host, self._port, self._fingerprint)
            try:
                result = await client.connect(pair=True, pairing_code=user_input.get(CONF_PAIRING_CODE) or None)
            except SlateAuthError as err:
                errors["base"] = PAIR_ERRORS.get(err.reason, "pairing_failed")
            except SlateConnectionError:
                errors["base"] = "cannot_connect"
            else:
                data = {CONF_HOST: self._host, CONF_PORT: self._port, CONF_FINGERPRINT: self._fingerprint,
                        CONF_TOKEN: result["token"], CONF_DEVICE_ID: self._hello["device_id"]}
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                return self.async_create_entry(title=self._hello.get("name") or "HA Slate", data=data)
            finally:
                await client.close()
        schema = probatio.Schema({probatio.Optional(CONF_PAIRING_CODE): str})
        return self.async_show_form(
            step_id="pair", data_schema=schema, errors=errors,
            description_placeholders={"name": self._hello.get("name", "HA Slate"),
                                      "fingerprint": (self._fingerprint or "")[:16]},
        )
