"""Text: the dashboard URL the kiosk browser shows."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .client import SlateError
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([SlateKioskUrl(entry.runtime_data)])


class SlateKioskUrl(SlateEntity, TextEntity):
    _attr_translation_key = "kiosk_url"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_pattern = r"^(https?://.+)?$"
    _attr_native_max = 255

    def __init__(self, device) -> None:
        super().__init__(device, "kiosk_url")

    @property
    def native_value(self) -> str | None:
        return self.device.settings.get("kiosk_url") or ""

    async def async_set_value(self, value: str) -> None:
        try:
            await self.device.async_set_settings(kiosk_url=value or None)
        except SlateError as err:
            raise HomeAssistantError(str(err)) from err
