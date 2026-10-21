"""Transient per-drum profile name inputs on the native device page."""

from __future__ import annotations

from typing import override

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity
from .protocol import Drum


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register local name inputs only when controls are enabled."""
    if not entry.runtime_data.controls_enabled:
        return
    async_add_entities(
        UplusRebornProfileName(entry.runtime_data, drum) for drum in ("upper", "lower")
    )


class UplusRebornProfileName(UplusRebornEntity, TextEntity):
    """Hold a non-restored name, not a saved record or executable draft."""

    _attr_native_min = 0
    _attr_native_max = 100
    _attr_icon = "mdi:form-textbox"

    def __init__(self, coordinator: UplusRebornCoordinator, drum: Drum) -> None:
        key = f"{drum}_profile_name"
        super().__init__(coordinator, key)
        self._drum = drum
        self._attr_translation_key = key

    @property
    @override
    def available(self) -> bool:
        """Local editing does not depend on appliance availability."""
        return self.coordinator.controls_enabled

    @property
    @override
    def native_value(self) -> str:
        return self.coordinator.profile_name(self._drum)

    @override
    async def async_set_value(self, value: str) -> None:
        try:
            self.coordinator.set_profile_name(self._drum, value)
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
