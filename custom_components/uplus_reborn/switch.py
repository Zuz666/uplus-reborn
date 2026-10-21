"""Local cycle option drafts and authoritative appliance settings."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity, async_execute_control
from .protocol import WASH_OPTION_BITS, Drum, available_intent_functions


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.runtime_data.controls_enabled:
        return
    async_add_entities(
        [
            UplusRebornDraftSwitch(entry.runtime_data, drum, option)
            for drum in ("upper", "lower")
            for option in WASH_OPTION_BITS
        ]
        + [
            UplusRebornApplianceSwitch(entry.runtime_data, option)
            for option in ("power", "buzzer", "child_lock")
        ]
    )


class UplusRebornDraftSwitch(UplusRebornEntity, SwitchEntity):
    """Stage an offered cycle option without any appliance write."""

    def __init__(
        self, coordinator: UplusRebornCoordinator, drum: Drum, option: str
    ) -> None:
        key = f"{drum}_{option}"
        super().__init__(coordinator, key)
        self._drum = drum
        self._option = option
        self._attr_translation_key = key
        self._attr_icon = "mdi:tune"

    @property
    @override
    def available(self) -> bool:
        draft = self.coordinator.draft(self._drum)
        return (
            super().available
            and self.coordinator.controls_enabled
            and draft is not None
            and self._option in available_intent_functions(draft)
        )

    @property
    @override
    def is_on(self) -> bool | None:
        draft = self.coordinator.draft(self._drum)
        return self._option in draft.enabled_functions if draft is not None else None

    def _stage(self, enabled: bool) -> None:
        if not self.available:
            raise ServiceValidationError(
                "Option is unavailable for the selected program"
            )
        draft = self.coordinator.draft(self._drum)
        assert draft is not None
        selected = set(draft.enabled_functions)
        if enabled:
            selected.add(self._option)
        else:
            selected.discard(self._option)
        try:
            self.coordinator.stage_settings(
                self._drum, enabled_functions=tuple(sorted(selected))
            )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        self._stage(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        self._stage(False)


class UplusRebornApplianceSwitch(UplusRebornEntity, SwitchEntity):
    """A real setting; changing it sends a command, unlike the draft switches."""

    def __init__(self, coordinator: UplusRebornCoordinator, option: str) -> None:
        super().__init__(coordinator, option)
        self._option = option
        self._attr_translation_key = option
        if option == "power":
            # Power-off loses physical Remote Control; no fictional LAN wake.
            self._attr_entity_registry_enabled_default = False
        elif option in ("buzzer", "child_lock"):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    @override
    def available(self) -> bool:
        return super().available and self.coordinator.controls_enabled

    @property
    @override
    def is_on(self) -> bool | None:
        attribute = "buzzer_enabled" if self._option == "buzzer" else self._option
        return getattr(self.coordinator.data, attribute)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_execute_control(
            self.coordinator.async_set_appliance_option(self._option, True)
        )

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_execute_control(
            self.coordinator.async_set_appliance_option(self._option, False)
        )
