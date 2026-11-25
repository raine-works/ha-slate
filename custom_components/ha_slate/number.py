"""Number: speaker volume."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([SlateVolume(entry.runtime_data)])


class SlateVolume(SlateEntity, NumberEntity):
    _attr_translation_key = "volume"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, device) -> None:
        super().__init__(device, "volume")

    @property
    def native_value(self) -> float | None:
        vol = self.device.settings.get("volume")
        return None if vol is None else round(vol * 100)

    async def async_set_native_value(self, value: float) -> None:
        await self.device.async_set_settings(volume=value / 100)
