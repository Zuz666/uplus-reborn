"""U+ Reborn local Home Assistant integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_DEVICE_ID, DOMAIN, PLATFORMS, configured_poll_interval
from .coordinator import UplusRebornCoordinator
from .profiles import ProfileStore
from .protocol import UwtClient
from .services import async_refresh_profile_actions, async_register_actions

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type UplusRebornConfigEntry = ConfigEntry[UplusRebornCoordinator]

# Only retired boolean selects change domain; ordinary selectors retain identity.
_OBSOLETE_SELECT_KEYS = tuple(
    f"{drum}_{option}"
    for drum in ("upper", "lower")
    for option in ("high_water", "easy_iron", "intensive", "eco", "soak", "night")
)
_BUTTON_MIGRATIONS = {
    "start_upper_spin_400": "upper_start",
    "cancel_upper_spin": "upper_cancel",
}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration actions independently of loaded entries."""
    async_register_actions(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UplusRebornConfigEntry) -> bool:
    """Set up one locally connected appliance."""
    registry = er.async_get(hass)
    obsolete_ids = {
        f"{entry.data[CONF_DEVICE_ID]}_{key}" for key in _OBSOLETE_SELECT_KEYS
    }
    # Also clean opted-out/offline upgrades, without touching retained identities.
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registered.domain == "select"
            and registered.platform == DOMAIN
            and registered.unique_id in obsolete_ids
        ):
            registry.async_remove(registered.entity_id)
        elif registered.domain == "button" and registered.platform == DOMAIN:
            for old, new in _BUTTON_MIGRATIONS.items():
                if registered.unique_id == f"{entry.data[CONF_DEVICE_ID]}_{old}":
                    registry.async_update_entity(
                        registered.entity_id,
                        new_unique_id=f"{entry.data[CONF_DEVICE_ID]}_{new}",
                    )
                    break

    connection_data = entry.data
    client = UwtClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        device_id=entry.data[CONF_DEVICE_ID],
    )
    coordinator = UplusRebornCoordinator(hass, entry, client)
    # Retain classified first-refresh failure context for cached diagnostics.
    entry.runtime_data = coordinator

    controls_enabled = coordinator.controls_enabled
    setup_complete = False
    control_surface_changed = False
    reload_scheduled = False

    async def async_update_entry(
        hass: HomeAssistant, updated_entry: UplusRebornConfigEntry
    ) -> None:
        """Reconcile saved settings without reloading inside the setup lock."""
        nonlocal control_surface_changed, reload_scheduled
        if updated_entry.state not in (
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.LOADED,
        ):
            return
        coordinator.update_interval = timedelta(
            seconds=configured_poll_interval(updated_entry.options)
        )
        enabled = coordinator.controls_enabled
        if not enabled:
            coordinator.clear_drafts()
        # A toggle during platform setup can leave a partial surface even if
        # another save restores the original opt-in value before setup finishes.
        control_surface_changed |= enabled != controls_enabled
        routing_changed = updated_entry.data != connection_data
        if routing_changed or control_surface_changed:
            coordinator.clear_drafts()
            if not reload_scheduled and (routing_changed or setup_complete):
                reload_scheduled = True
                hass.config_entries.async_schedule_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(async_update_entry))
    try:
        await coordinator.profiles.async_load()
    except ValueError as error:
        raise ConfigEntryError(str(error)) from error
    # Listen before either setup await; Core removes this listener if setup fails.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # A configured washer may normally be switched off. Register unavailable
        # entities so their listeners keep ordinary coordinator polling active.
        error = coordinator.health.last_error
        if error is None or error.cause not in {
            "connection_closed",
            "connection_error",
            "timeout",
        }:
            raise
    await async_update_entry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    setup_complete = True
    await async_update_entry(hass, entry)
    await async_refresh_profile_actions(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: UplusRebornConfigEntry
) -> bool:
    """Unload an appliance entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await async_refresh_profile_actions(hass)
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: UplusRebornConfigEntry
) -> None:
    """Delete only this entry's persisted profiles on explicit entry removal."""
    await ProfileStore(hass, entry.entry_id, entry.data[CONF_DEVICE_ID]).async_remove()
