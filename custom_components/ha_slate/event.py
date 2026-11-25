"""Event entity: fires on every on-device wake word detection (for automations)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .const import EVENT_WAKE_WORD_DETECTED
from .device import SlateDevice
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Add the wake word event entity."""
    async_add_entities([SlateWakeWordEvent(entry.runtime_data)])


class SlateWakeWordEvent(SlateEntity, EventEntity):
    """Fires ``detected``, with the wake word and its probability, each time this panel hears a wake word."""

    _attr_translation_key = "wake_word"
    _attr_event_types = ["detected"]

    def __init__(self, device: SlateDevice) -> None:
        """Create the entity for ``device``."""
        super().__init__(device, "wake_word_event")

    async def async_added_to_hass(self) -> None:
        """Listen for the satellite's wake word bus events (this panel's only) until removed."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_WAKE_WORD_DETECTED, self._on_event))

    @callback
    def _on_event(self, event: Event) -> None:
        if event.data.get("device_id") != (self.registry_entry.device_id if self.registry_entry else None):
            return
        self._trigger_event("detected", {"wake_word_id": event.data.get("wake_word_id"),
                                         "wake_word_phrase": event.data.get("wake_word_phrase"),
                                         "probability": event.data.get("probability")})
        self.async_write_ha_state()
