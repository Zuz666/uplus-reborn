"""Atomic program submission without mutating shared UI drafts."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import UplusRebornCoordinator
from .entity import async_execute_control
from .protocol import WASH_OPTION_BITS, default_intent, fabric_intent, update_intent


def _integer(value: object) -> int:
    if type(value) is not int:
        raise vol.Invalid("Expected a whole number")
    return value


_START_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("device_id"): cv.string,
            vol.Required("drum"): vol.In(("upper", "lower")),
            vol.Exclusive("program", "selection"): cv.string,
            vol.Exclusive("fabric", "selection"): cv.string,
            vol.Optional("wash_minutes"): vol.All(_integer, vol.Range(min=0, max=255)),
            vol.Optional("temperature_c"): vol.All(_integer, vol.Range(min=0, max=100)),
            vol.Optional("rinses"): vol.All(_integer, vol.Range(min=0, max=8)),
            vol.Optional("spin_rpm"): vol.All(_integer, vol.Range(min=0, max=1500)),
            vol.Optional("drying_mode"): cv.string,
            vol.Optional("delay_hours"): vol.All(_integer, vol.Range(min=0, max=24)),
            vol.Optional("enabled_functions"): [vol.In(tuple(WASH_OPTION_BITS))],
            vol.Optional("stains"): [cv.string],
        }
    ),
    cv.has_at_least_one_key("program", "fabric"),
)

_PROFILE_TARGET = {
    vol.Required("device_id"): cv.string,
    vol.Required("drum"): vol.In(("upper", "lower")),
}
_PROFILE_SCHEMA = vol.Schema(
    {
        **_PROFILE_TARGET,
        vol.Required("profile_id"): cv.string,
    }
)
_SAVE_PROFILE_SCHEMA = vol.Schema(
    {
        **_PROFILE_TARGET,
        vol.Required("name"): cv.string,
    }
)
_RENAME_PROFILE_SCHEMA = _PROFILE_SCHEMA.extend({vol.Required("name"): cv.string})


def _coordinator(hass: HomeAssistant, device_id: str) -> UplusRebornCoordinator:
    """Resolve exactly one loaded owner, including on minimum Core."""
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if device is not None
        and entry.state is ConfigEntryState.LOADED
        and any(
            candidate.id == device.id
            for candidate in dr.async_entries_for_config_entry(registry, entry.entry_id)
        )
    ]
    if len(entries) != 1:
        raise ServiceValidationError("Select a device with one loaded U+ Reborn entry")
    return entries[0].runtime_data


@callback
def async_register_actions(hass: HomeAssistant) -> None:
    """Register even when entries are unloaded so automations remain editable."""

    async def start_program(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call.data["device_id"])
        drum = call.data["drum"]
        changes = {
            key: value
            for key, value in call.data.items()
            if key not in ("device_id", "drum", "program", "fabric")
        }
        if "delay_hours" in changes:
            changes["delay_half_hour_step"] = changes.pop("delay_hours") * 2
        if "enabled_functions" in changes:
            changes["enabled_functions"] = tuple(changes["enabled_functions"])
        if "stains" in changes:
            changes["stains"] = tuple(changes["stains"])
        try:
            if "fabric" in call.data:
                stains = changes.pop("stains", ())
                if changes:
                    raise ValueError(
                        "Smart fabric recipes do not offer manual setting overrides"
                    )
                intent = fabric_intent(drum, call.data["fabric"], stains)
            else:
                intent = update_intent(
                    default_intent(drum, call.data["program"]), **changes
                )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        await async_execute_control(coordinator.async_start_program(drum, intent))

    hass.services.async_register(
        DOMAIN, "start_program", start_program, schema=_START_SCHEMA
    )

    async def profile_action(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call.data["device_id"])
        drum = call.data["drum"]
        profile_id = call.data.get("profile_id")
        profile = None
        try:
            if call.service == "save_profile":
                profile = await coordinator.async_save_profile(drum, call.data["name"])
            elif call.service == "update_profile":
                profile = await coordinator.async_update_profile(drum, profile_id)
            elif call.service == "rename_profile":
                profile = await coordinator.async_rename_profile(
                    drum, profile_id, call.data["name"]
                )
            elif call.service == "delete_profile":
                await coordinator.async_delete_profile(drum, profile_id)
            elif call.service == "apply_profile":
                coordinator.apply_profile(drum, profile_id)
            elif call.service == "start_profile":
                await async_execute_control(
                    coordinator.async_start_profile(drum, profile_id)
                )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        if call.return_response and profile is not None:
            return {
                "profile_id": profile.id,
                "name": profile.name,
                "drum": profile.drum,
                "revision": profile.revision,
            }
        return None

    for action, schema in (
        ("save_profile", _SAVE_PROFILE_SCHEMA),
        ("update_profile", _PROFILE_SCHEMA),
        ("rename_profile", _RENAME_PROFILE_SCHEMA),
        ("delete_profile", _PROFILE_SCHEMA),
        ("apply_profile", _PROFILE_SCHEMA),
        ("start_profile", _PROFILE_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            action,
            profile_action,
            schema=schema,
            supports_response=(
                SupportsResponse.OPTIONAL
                if action in ("save_profile", "update_profile", "rename_profile")
                else SupportsResponse.NONE
            ),
        )
