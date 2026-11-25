"""Selects: Assist pipeline, VAD sensitivity, wake word sensitivity and location."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import AssistPipelineSelect, VadSensitivitySelect
from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .const import DOMAIN
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    device = entry.runtime_data
    async_add_entities([
        SlatePipelineSelect(hass, device),
        SlateVadSensitivitySelect(hass, device),
        SlateSettingSelect(device, "wake_word_sensitivity", ["default", "more_sensitive", "less_sensitive"]),
        SlateSettingSelect(device, "wake_word_location", ["device", "home_assistant"]),
    ])


class SlatePipelineSelect(SlateEntity, AssistPipelineSelect):
    """Which Assist pipeline (STT/agent/TTS, and HA-side wake word) the panel uses."""

    def __init__(self, hass: HomeAssistant, device) -> None:
        SlateEntity.__init__(self, device, "pipeline")
        AssistPipelineSelect.__init__(self, hass, DOMAIN, device.device_id)


class SlateVadSensitivitySelect(SlateEntity, VadSensitivitySelect):
    """How long a pause ends the user's sentence ("finished speaking detection")."""

    def __init__(self, hass: HomeAssistant, device) -> None:
        SlateEntity.__init__(self, device, "vad_sensitivity")
        VadSensitivitySelect.__init__(self, hass, device.device_id)


class SlateSettingSelect(SlateEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device, key: str, options: list[str]) -> None:
        super().__init__(device, key)
        self._key = key
        self._attr_translation_key = key
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        return self.device.settings.get(self._key)

    async def async_select_option(self, option: str) -> None:
        await self.device.async_set_settings(**{self._key: option})
