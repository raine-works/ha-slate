"""Buttons: reboot, reload the dashboard."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    device = entry.runtime_data
    async_add_entities([SlateButton(device, "reboot", ButtonDeviceClass.RESTART),
                        SlateButton(device, "reload_kiosk", None)])


class SlateButton(SlateEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device, command: str, device_class: ButtonDeviceClass | None) -> None:
        super().__init__(device, command)
        self._command = command.replace("_", "-")
        self._attr_translation_key = command
        self._attr_device_class = device_class

    async def async_press(self) -> None:
        await self.device.async_command(self._command)
