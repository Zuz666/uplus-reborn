"""Native action choices shared by profile completion and entry lifecycle."""

from __future__ import annotations

from copy import deepcopy

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.service import (
    async_get_all_descriptions,
    async_set_service_schema,
)

from .const import DOMAIN

_PROFILE_ACTIONS = (
    "update_profile",
    "rename_profile",
    "delete_profile",
    "apply_profile",
    "start_profile",
)


async def async_refresh_profile_actions(hass: HomeAssistant) -> None:
    """Populate native action dropdowns with stable IDs and editable labels."""
    descriptions = await async_get_all_descriptions(hass)
    options = [
        {
            "value": profile.id,
            "label": f"{entry.title} / {profile.drum} / {profile.name}"
            + (" (incompatible)" if profile.intent is None else ""),
        }
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
        and hasattr(entry, "runtime_data")
        for profile in entry.runtime_data.profiles.profiles()
    ]
    for action in _PROFILE_ACTIONS:
        if action not in descriptions.get(DOMAIN, {}):
            continue
        description = deepcopy(descriptions[DOMAIN][action])
        description["fields"]["profile_id"]["selector"] = {
            "select": {"options": options, "custom_value": True, "mode": "dropdown"}
        }
        async_set_service_schema(hass, DOMAIN, action, description)
        hass.bus.async_fire(
            EVENT_SERVICE_REGISTERED, {"domain": DOMAIN, "service": action}
        )
