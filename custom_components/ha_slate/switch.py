"""Switches: microphone mute, wake chime."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    device = entry.runtime_data
    async_add_entities([SlateSettingSwitch(device, "muted", None), SlateSettingSwitch(device, "wake_sound", EntityCategory.CONFIG)])


class SlateSettingSwitch(SlateEntity, SwitchEntity):
    def __init__(self, device, key: str, category: EntityCategory | None) -> None:
        super().__init__(device, key)
        self._key = key
        self._attr_translation_key = key
        self._attr_entity_category = category

    @property
    def is_on(self) -> bool | None:
        return self.device.settings.get(self._key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.device.async_set_settings(**{self._key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.device.async_set_settings(**{self._key: False})
