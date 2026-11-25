"""Selects: Assist pipeline, VAD sensitivity, wake word sensitivity and location."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import AssistPipelineSelect, VadSensitivitySelect
from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .const import DOMAIN
from .device import SlateDevice
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Add the pipeline, VAD sensitivity and wake word sensitivity and location selects."""
    device = entry.runtime_data
    async_add_entities([
        SlatePipelineSelect(hass, device),
        SlateVadSensitivitySelect(hass, device),
        SlateSettingSelect(device, "wake_word_sensitivity", ["default", "more_sensitive", "less_sensitive"]),
        SlateSettingSelect(device, "wake_word_location", ["device", "home_assistant"]),
    ])


class SlatePipelineSelect(SlateEntity, AssistPipelineSelect):
    """Which Assist pipeline (STT/agent/TTS, and HA-side wake word) the panel uses."""

    def __init__(self, hass: HomeAssistant, device: SlateDevice) -> None:
        """Create the select; HA's assist_pipeline stores the choice, keyed by the panel's device ID."""
        SlateEntity.__init__(self, device, "pipeline")
        AssistPipelineSelect.__init__(self, hass, DOMAIN, device.device_id)


class SlateVadSensitivitySelect(SlateEntity, VadSensitivitySelect):
    """How long a pause ends the user's sentence ("finished speaking detection")."""

    def __init__(self, hass: HomeAssistant, device: SlateDevice) -> None:
        """Create the select; HA's assist_pipeline stores the choice, keyed by the panel's device ID."""
        SlateEntity.__init__(self, device, "vad_sensitivity")
        VadSensitivitySelect.__init__(self, hass, device.device_id)


class SlateSettingSelect(SlateEntity, SelectEntity):
    """A select backed by one string in the panel's settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: SlateDevice, key: str, options: list[str]) -> None:
        """Create the select for the panel setting ``key`` (also its entity key and translation key).

        ``options`` must match the values the agent accepts for that setting.
        """
        super().__init__(device, key)
        self._key = key
        self._attr_translation_key = key
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        """The setting's last reported value, or None before the panel has sent its state."""
        return self.device.settings.get(self._key)

    async def async_select_option(self, option: str) -> None:
        """Set the setting; raises ``SlateError`` if the panel refuses it or isn't connected."""
        await self.device.async_set_settings(**{self._key: option})
