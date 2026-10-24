"""Per-drum appliance actions and local profile saving for U+ Reborn."""

from __future__ import annotations

from typing import override

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity, async_execute_control
from .protocol import Drum, UnsafeState, validate_drum_control, validate_program_start

_ACTION_ICONS = {
    "start": "mdi:play",
    "pause": "mdi:pause",
    "resume": "mdi:play-pause",
    "cancel": "mdi:stop",
    "reset": "mdi:restart",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register both drums' actions when controls are enabled."""
    if not entry.runtime_data.controls_enabled:
        return
    async_add_entities(
        UplusRebornButton(entry.runtime_data, drum, action)
        for drum in ("upper", "lower")
        for action in _ACTION_ICONS
    )
    async_add_entities(
        UplusRebornSaveProfileButton(entry.runtime_data, drum)
        for drum in ("upper", "lower")
    )


class UplusRebornButton(UplusRebornEntity, ButtonEntity):
    """Dispatch one named action and require authoritative outcome evidence."""

    def __init__(
        self, coordinator: UplusRebornCoordinator, drum: Drum, action: str
    ) -> None:
        key = f"{drum}_{action}"
        super().__init__(coordinator, key)
        self._drum = drum
        self._action = action
        self._attr_translation_key = key
        self._attr_icon = _ACTION_ICONS[action]

    @property
    @override
    def available(self) -> bool:
        if not super().available or not self.coordinator.controls_enabled:
            return False
        try:
            if self._action == "start":
                draft = self.coordinator.draft(self._drum)
                if draft is None:
                    return False
                validate_program_start(self.coordinator.data, draft)
            else:
                validate_drum_control(self.coordinator.data, self._drum, self._action)
        except (UnsafeState, ValueError):
            return False
        return True

    @override
    async def async_press(self) -> None:
        await async_execute_control(
            self.coordinator.async_start_program(self._drum)
            if self._action == "start"
            else self.coordinator.async_control_drum(self._drum, self._action),
            pause_resume=self._action in ("pause", "resume"),
        )


class UplusRebornSaveProfileButton(UplusRebornEntity, ButtonEntity):
    """Persist the named complete draft without any appliance interaction."""

    _attr_icon = "mdi:content-save-plus"

    def __init__(self, coordinator: UplusRebornCoordinator, drum: Drum) -> None:
        key = f"{drum}_save_profile"
        super().__init__(coordinator, key)
        self._drum = drum
        self._attr_translation_key = key

    @property
    @override
    def available(self) -> bool:
        """Local saving does not require an available appliance."""
        return self.coordinator.controls_enabled

    @override
    async def async_press(self) -> None:
        try:
            await self.coordinator.async_save_profile(
                self._drum, self.coordinator.profile_name(self._drum)
            )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
