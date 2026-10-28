"""Expose cached, device-identifier-free information through native System Health."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DOMAIN, configured_poll_interval


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the native platform callback without probing the appliance."""
    register.async_register_info(
        system_health_info, manage_url="/config/integrations"
    )


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Summarize lifecycle and cached health, never clients or entry identifiers."""
    integration = await async_get_integration(hass, DOMAIN)
    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False)
    info: dict[str, Any] = {
        "integration_version": integration.version,
        "configured_entries": len(entries),
        "loaded_entries": 0,
        "available_entries": 0,
        "disabled_entries": 0,
        "unloaded_entries": 0,
        "polling_mode": "local_polling",
    }
    for index, entry in enumerate(entries, start=1):
        loaded = entry.state is ConfigEntryState.LOADED
        disabled = entry.disabled_by is not None
        unloaded = entry.state is ConfigEntryState.NOT_LOADED
        info["loaded_entries"] += int(loaded)
        info["disabled_entries"] += int(disabled)
        info["unloaded_entries"] += int(unloaded and not disabled)

        # An old runtime can survive a failed setup/unload. It is not evidence
        # that HA loaded the entry or that disabled/unloaded polling is active.
        coordinator = getattr(entry, "runtime_data", None)
        health = (
            coordinator.health
            if coordinator is not None and not disabled and not unloaded
            else None
        )
        available = bool(loaded and health is not None and health.available)
        info["available_entries"] += int(available)
        if disabled:
            freshness = "disabled"
        elif unloaded:
            freshness = "unloaded"
        elif health is None or health.last_success is None:
            freshness = "never_read"
        elif not loaded:
            freshness = "unavailable"
        else:
            freshness = health.freshness

        # Ordinals are report-local labels, not stable or device-derived IDs.
        prefix = f"entry_{index}"
        info[f"{prefix}_state"] = entry.state.value
        info[f"{prefix}_available"] = available
        info[f"{prefix}_poll_interval_seconds"] = configured_poll_interval(
            entry.options
        )
        info[f"{prefix}_freshness"] = freshness
        info[f"{prefix}_last_success_age_seconds"] = (
            health.age_seconds if health is not None else None
        )
        info[f"{prefix}_last_failure_category"] = (
            health.last_error.cause
            if health is not None and health.last_error is not None
            else None
        )
        failure = health.last_error if health is not None else None
        for key in ("stage", "request_id", "session_id", "elapsed_ms"):
            info[f"{prefix}_last_failure_{key}"] = (
                getattr(failure, key, None) if failure is not None else None
            )
        capture = (
            coordinator.capture_status
            if loaded and not disabled and coordinator is not None
            else None
        )
        if disabled:
            capture_state = "disabled"
        elif unloaded:
            capture_state = "unloaded"
        elif capture is None:
            capture_state = "unavailable"
        else:
            capture_state = capture["capture_state"]
        marker = {
            "recording": "🟢",
            "stopped": "⚪",
            "closed": "⚫",
            "disabled": "⚫",
            "unloaded": "⚫",
            "unavailable": "⚪",
        }[capture_state]
        info[f"{prefix}_capture_status_snapshot"] = (
            f"{marker} {capture_state} (snapshot)"
        )
        info[f"{prefix}_capture_snapshot_at"] = (
            capture["snapshot_at"] if capture is not None
            else dt_util.utcnow().isoformat()
        )
        for key in (
            "started_at", "end_at", "stopped_at", "stop_reason",
            "remaining_seconds", "debug_enabled", "first_retained_at",
            "last_recorded_at", "partial_operations",
        ):
            info[f"{prefix}_capture_{key}"] = (
                capture.get(key) if capture is not None else None
            )
        for resource in ("operations", "events", "raw_bytes"):
            limit = capture["limits"][resource] if capture is not None else None
            retained = capture["retained"][resource] if capture is not None else None
            info[f"{prefix}_capture_{resource}_limit"] = limit
            info[f"{prefix}_capture_{resource}_retained"] = retained
            info[f"{prefix}_capture_{resource}_free"] = (
                max(0, limit - retained)
                if limit is not None and retained is not None else None
            )
            info[f"{prefix}_capture_evicted_operations_by_{resource}_limit"] = (
                capture.get("evicted_operations_by_limit", {}).get(resource)
                if capture is not None else None
            )
        for resource in ("operations", "events", "raw_bytes", "metadata"):
            info[f"{prefix}_capture_omitted_{resource}"] = (
                capture["omitted"][resource] if capture is not None else None
            )
    return info
