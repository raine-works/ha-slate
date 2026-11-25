"""Base entity for HA Slate."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .device import SlateDevice


class SlateEntity(Entity):
    """Base for every HA Slate entity: one panel's device info, availability and push updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: SlateDevice, key: str) -> None:
        """Attach the entity to ``device``; its unique ID is ``<device_id>-<key>``.

        The device ID doubles as a MAC connection when it's 12 lowercase hex digits.
        """
        self.device = device
        self._attr_unique_id = f"{device.device_id}-{key}"
        mac = device.device_id
        connections = set()
        if len(mac) == 12 and all(c in "0123456789abcdef" for c in mac):
            connections.add((CONNECTION_NETWORK_MAC, ":".join(mac[i:i + 2] for i in range(0, 12, 2))))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            connections=connections,
            name=device.hello.get("name") or "HA Slate",
            manufacturer="HA Slate",
            model=device.hello.get("model", "HA Slate"),
            sw_version=device.hello.get("sw_version"),
        )

    @property
    def available(self) -> bool:
        """Available while the panel's Slate Link connection is up."""
        return self.device.connected

    async def async_added_to_hass(self) -> None:
        """Write the entity's state whenever the device pushes an update, until removed."""
        await super().async_added_to_hass()
        self.async_on_remove(self.device.async_add_listener(self._handle_device_update))

    def _handle_device_update(self) -> None:
        self.async_write_ha_state()
