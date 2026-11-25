"""Buttons: reboot, reload the dashboard."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .device import SlateDevice
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Add the panel's reboot and reload-dashboard buttons."""
    device = entry.runtime_data
    async_add_entities([SlateButton(device, "reboot", ButtonDeviceClass.RESTART),
                        SlateButton(device, "reload_kiosk", None)])


class SlateButton(SlateEntity, ButtonEntity):
    """A button that sends one Slate Link command to the panel when pressed."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: SlateDevice, command: str, device_class: ButtonDeviceClass | None) -> None:
        """Create the button.

        ``command`` is the entity key in snake_case (also its translation key); the
        panel command sent is the same name in kebab-case (``reload_kiosk`` sends
        ``reload-kiosk``).
        """
        super().__init__(device, command)
        self._command = command.replace("_", "-")
        self._attr_translation_key = command
        self._attr_device_class = device_class

    async def async_press(self) -> None:
        """Send the command; raises the device's ``SlateError`` if the panel refuses it or isn't connected."""
        await self.device.async_command(self._command)
