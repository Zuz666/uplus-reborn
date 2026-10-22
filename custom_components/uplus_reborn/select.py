"""Native, local-only program and setting drafts for both drums."""

from __future__ import annotations

from typing import override

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity
from .protocol import (
    PROGRAM_CAPABILITIES,
    Drum,
    fabric_capabilities,
    fabric_stains,
    intent_capabilities,
    intent_wash_stage_minutes,
    ordinary_stain_capability,
    ordinary_stain_choices,
    program_delay_capabilities,
    program_drying_modes,
)

_FIELDS = {
    "program_selection": "program",
    "fabric": "fabric",
    "wash_minutes": "wash_minutes",
    "temperature": "temperature_c",
    "rinses": "rinses",
    "spin_speed": "spin_rpm",
    "delay": "delay_half_hour_step",
    "stain": "stains",
    "drying": "drying_mode",
}
_ALLOWED = {
    "wash_minutes": "allowed_wash_minutes",
    "temperature": "allowed_temperatures_c",
    "rinses": "allowed_rinses",
    "spin_speed": "allowed_spin_rpm",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register drafts only when remote controls are enabled."""
    coordinator = entry.runtime_data
    if not coordinator.controls_enabled:
        return
    async_add_entities(
        UplusRebornSelect(coordinator, drum, setting)
        for drum in ("upper", "lower")
        for setting in _FIELDS
        if drum == "lower" or setting != "drying"
    )
    async_add_entities(
        UplusRebornProfileSelect(coordinator, drum) for drum in ("upper", "lower")
    )


class UplusRebornSelect(UplusRebornEntity, SelectEntity):
    """Select source-offered values without claiming they are applied state."""

    def __init__(
        self, coordinator: UplusRebornCoordinator, drum: Drum, setting: str
    ) -> None:
        key = f"{drum}_{setting}"
        super().__init__(coordinator, key)
        self._drum = drum
        self._setting = setting
        self._attr_translation_key = key
        if setting == "program_selection":
            self._attr_icon = "mdi:washing-machine"
        elif setting == "fabric":
            self._attr_icon = "mdi:auto-fix"
        else:
            self._attr_icon = "mdi:tune"

    @property
    @override
    def options(self) -> list[str]:
        if self._setting == "program_selection":
            return ["none", *PROGRAM_CAPABILITIES[self._drum]]
        if self._setting == "fabric":
            return [
                "none",
                *(item.ui_id.lower() for item in fabric_capabilities(self._drum)),
            ]
        draft = self.coordinator.draft(self._drum)
        if draft is None:
            return []
        if self._setting == "stain":
            if draft.fabric is None:
                choices = ordinary_stain_choices(self._drum, draft.program)
                return ["none", *choices] if choices else []
            return [
                "none",
                *(row.ui_id.lower() for row in fabric_stains(self._drum, draft.fabric)),
            ]
        if draft.fabric is not None:
            return []
        if self._setting in _ALLOWED:
            values = getattr(intent_capabilities(draft), _ALLOWED[self._setting])
            # The source picker disables fixed scalar lists, including Tub Clean.
            return [str(value) for value in values] if len(values) > 1 else []
        if self._setting == "drying":
            return [mode.name for mode in program_drying_modes(draft.program)]
        if self._setting == "delay":
            return [
                "off" if step == 0 else str(step // 2)
                for step in program_delay_capabilities(
                    self._drum, draft.program
                ).available_half_hour_steps
            ]
        return []

    @property
    @override
    def current_option(self) -> str | None:
        draft = self.coordinator.draft(self._drum)
        if draft is None:
            return "none" if self._setting in ("program_selection", "fabric") else None
        value = getattr(draft, _FIELDS[self._setting])
        if self._setting == "delay":
            return "off" if not value else str(value // 2)
        if self._setting == "stain":
            return value[0].lower() if value else "none"
        if self._setting == "fabric":
            return value.lower() if value else "none"
        return str(value) if value is not None else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Expose the next-run ordinary treatment, never appliance readback."""
        if self._setting != "stain":
            return None
        draft = self.coordinator.draft(self._drum)
        if draft is None or draft.fabric is not None or not draft.stains:
            return None
        treatment = ordinary_stain_capability(
            self._drum, draft.program, draft.stains[0]
        )
        return {
            "next_run_temperature_c": treatment.temperature_c,
            "next_run_wash_stage_minutes": intent_wash_stage_minutes(draft),
        }

    @property
    @override
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.controls_enabled
            and bool(self.options)
        )

    @override
    async def async_select_option(self, option: str) -> None:
        if not self.coordinator.controls_enabled:
            raise ServiceValidationError("Remote controls are disabled")
        if option not in self.options:
            raise ServiceValidationError(
                "Setting is not offered for the selected program"
            )
        try:
            if self._setting == "program_selection":
                self.coordinator.stage_program(self._drum, option)
                return
            if self._setting == "fabric":
                self.coordinator.stage_fabric(
                    self._drum, "none" if option == "none" else option.upper()
                )
                return
            value: str | int | tuple[str, ...] = option
            if self._setting in _ALLOWED:
                value = int(option)
            elif self._setting == "delay":
                value = 0 if option == "off" else int(option) * 2
            elif self._setting == "stain":
                draft = self.coordinator.draft(self._drum)
                if option != "none" and draft is not None and draft.fabric is not None:
                    option = option.upper()
                value = () if option == "none" else (option,)
            self.coordinator.stage_settings(
                self._drum, **{_FIELDS[self._setting]: value}
            )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error


class UplusRebornProfileSelect(UplusRebornEntity, SelectEntity):
    """Restore a named profile locally; editable labels never become identity."""

    def __init__(self, coordinator: UplusRebornCoordinator, drum: Drum) -> None:
        super().__init__(coordinator, f"{drum}_profile")
        self._drum = drum
        self._attr_translation_key = f"{drum}_profile"
        self._attr_icon = "mdi:content-save-settings"

    @property
    @override
    def options(self) -> list[str]:
        return [
            "none",
            *(
                profile.name
                for profile in self.coordinator.profiles.profiles(self._drum)
            ),
        ]

    @property
    @override
    def current_option(self) -> str:
        selected = self.coordinator.selected_profile(self._drum)
        return next(
            (
                profile.name
                for profile in self.coordinator.profiles.profiles(self._drum)
                if profile.id == selected
            ),
            "none",
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "profile_id": self.coordinator.selected_profile(self._drum),
            "profiles": {
                profile.name: profile.id
                for profile in self.coordinator.profiles.profiles(self._drum)
            },
        }

    @property
    @override
    def available(self) -> bool:
        # Profile restoration is local and does not require an online appliance.
        return self.coordinator.controls_enabled

    @override
    async def async_select_option(self, option: str) -> None:
        if not self.coordinator.controls_enabled:
            raise ServiceValidationError("Remote controls are disabled")
        if option == "none":
            self.coordinator.detach_profile(self._drum)
            return
        profile = next(
            (
                profile
                for profile in self.coordinator.profiles.profiles(self._drum)
                if profile.name == option
            ),
            None,
        )
        if profile is None:
            raise ServiceValidationError("Profile not found; refresh the profile list")
        try:
            self.coordinator.apply_profile(self._drum, profile.id)
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
