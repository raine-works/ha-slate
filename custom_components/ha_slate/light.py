"""Lights: the edge light bar (RGB + effects) and the display backlight."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_RGB_COLOR, ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .const import EDGE_EFFECTS
from .device import SlateDevice
from .entity import SlateEntity


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Add the edge light, and the display backlight when the panel reports one."""
    device = entry.runtime_data
    entities: list[LightEntity] = [SlateEdgeLight(device)]
    if "display" in device.state.get("lights", {}):
        entities.append(SlateDisplayLight(device))
    async_add_entities(entities)


class _SlateLight(SlateEntity, LightEntity):
    """Common state for a light the panel reports under ``state["lights"][target]``."""

    target: str

    @property
    def _light(self) -> dict[str, Any]:
        return self.device.state.get("lights", {}).get(self.target, {})

    @property
    def is_on(self) -> bool | None:
        """Whether the light is on, or None before the panel has reported it."""
        return self._light.get("state")

    @property
    def brightness(self) -> int | None:
        """Brightness as 0–255 (the panel reports 0–1), or None if unreported."""
        b = self._light.get("brightness")
        return None if b is None else round(float(b) * 255)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off; raises ``SlateError`` if the panel refuses it or isn't connected."""
        await self.device.async_set_light(self.target, state=False)


class SlateEdgeLight(_SlateLight):
    """User ambient colour; voice/timer/alert states temporarily override it."""

    target = "edge"
    _attr_translation_key = "edge_light"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = EDGE_EFFECTS

    def __init__(self, device: SlateDevice) -> None:
        """Create the edge light entity for ``device``."""
        super().__init__(device, "edge_light")

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """The user colour as 0–255 RGB (the panel reports 0–1), or None before it has reported one."""
        l = self._light
        if "red" not in l:
            return None
        return (round(l["red"] * 255), round(l["green"] * 255), round(l["blue"] * 255))

    @property
    def effect(self) -> str | None:
        """The active effect, one of ``EDGE_EFFECTS``, or None if unreported."""
        return self._light.get("effect")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, applying any brightness, RGB colour and effect given; raises ``SlateError`` on failure."""
        state: dict[str, Any] = {"state": True}
        if ATTR_BRIGHTNESS in kwargs:
            state["brightness"] = kwargs[ATTR_BRIGHTNESS] / 255
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            state.update(red=r / 255, green=g / 255, blue=b / 255)
        if ATTR_EFFECT in kwargs:
            state["effect"] = kwargs[ATTR_EFFECT]
        await self.device.async_set_light(self.target, **state)


class SlateDisplayLight(_SlateLight):
    """Screen backlight. Off = panel dark (kiosk keeps running)."""

    target = "display"
    _attr_translation_key = "display"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, device: SlateDevice) -> None:
        """Create the backlight entity for ``device``."""
        super().__init__(device, "display")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, at the brightness given if any; raises ``SlateError`` on failure."""
        state: dict[str, Any] = {"state": True}
        if ATTR_BRIGHTNESS in kwargs:
            state["brightness"] = kwargs[ATTR_BRIGHTNESS] / 255
        await self.device.async_set_light(self.target, **state)
