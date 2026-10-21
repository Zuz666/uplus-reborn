"""Behavioral coverage for cached, anonymous native System Health reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.uplus_reborn.coordinator import UplusRebornCoordinator
from custom_components.uplus_reborn.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    DeviceSnapshot,
    UwtClient,
)
from custom_components.uplus_reborn.system_health import system_health_info

from .conftest import DEVICE_ID, HOST

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PRIVATE_TITLE = "private-appliance-title"
PRIVATE_REASON = "private-account@example.invalid private-token"


def _entry(
    hass: HomeAssistant,
    *,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    disabled: bool = False,
    interval: object = 15,
) -> MockConfigEntry:
    """Create a lifecycle fixture without setting up entities or polling."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=PRIVATE_TITLE,
        data={CONF_HOST: HOST, CONF_DEVICE_ID: DEVICE_ID},
        unique_id=DEVICE_ID,
        options={CONF_POLL_INTERVAL: interval},
        state=state,
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
        reason=PRIVATE_REASON,
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> UplusRebornCoordinator:
    coordinator = UplusRebornCoordinator(hass, entry, UwtClient(HOST, DEVICE_ID))
    entry.runtime_data = coordinator
    return coordinator


async def test_empty_health_uses_manifest_version(hass: HomeAssistant) -> None:
    """No entries is a useful zero-count report, not a failing health probe."""
    info = await system_health_info(hass)
    manifest = json.loads(
        (
            Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert info["integration_version"] == manifest["version"]
    assert info["polling_mode"] == "local_polling"
    for key in (
        "configured_entries",
        "loaded_entries",
        "available_entries",
        "disabled_entries",
        "unloaded_entries",
    ):
        assert info[key] == 0
    assert not any(key.startswith("entry_") for key in info)


@pytest.mark.parametrize("state", list(ConfigEntryState))
@pytest.mark.parametrize("disabled", [False, True])
async def test_lifecycle_overrides_retained_successful_runtime(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    state: ConfigEntryState,
    disabled: bool,
) -> None:
    """A retained success cannot make a disabled or non-loaded entry available."""
    entry = _entry(hass, state=state, disabled=disabled)
    coordinator = _coordinator(hass, entry)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)),
        patch("custom_components.uplus_reborn.coordinator.datetime") as clock,
    ):
        clock.now.return_value = NOW
        await coordinator.async_refresh()
        info = await system_health_info(hass)

    loaded = state is ConfigEntryState.LOADED
    unloaded = state is ConfigEntryState.NOT_LOADED
    assert coordinator.health.last_success == NOW
    assert info["configured_entries"] == 1
    assert info["loaded_entries"] == int(loaded)
    assert info["available_entries"] == int(loaded and not disabled)
    assert info["disabled_entries"] == int(disabled)
    assert info["unloaded_entries"] == int(unloaded and not disabled)
    assert info["entry_1_state"] == state.value
    assert info["entry_1_available"] is (loaded and not disabled)
    if disabled or unloaded:
        assert info["entry_1_freshness"] == ("disabled" if disabled else "unloaded")
        assert info["entry_1_last_success_age_seconds"] is None
    else:
        assert info["entry_1_freshness"] == ("fresh" if loaded else "unavailable")
        assert info["entry_1_last_success_age_seconds"] == 0


async def test_multi_entry_ages_failures_and_recovery_remain_distinct(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A newer healthy entry cannot hide another entry's stale or failed state."""
    entries = [_entry(hass, interval=interval) for interval in (15, 30)]
    entries.append(_entry(hass, state=ConfigEntryState.SETUP_RETRY))
    coordinators = [_coordinator(hass, entry) for entry in entries]
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)),
        patch("custom_components.uplus_reborn.coordinator.datetime") as clock,
    ):
        clock.now.return_value = NOW
        await coordinators[0].async_refresh()
        clock.now.return_value = NOW + timedelta(seconds=20)
        await coordinators[1].async_refresh()
        clock.now.return_value = NOW + timedelta(seconds=31)
        info = await system_health_info(hass)
        assert info["configured_entries"] == 3
        assert info["loaded_entries"] == 2
        assert info["available_entries"] == 2
        assert info["entry_1_freshness"] == "stale"
        assert info["entry_1_last_success_age_seconds"] == 31
        assert info["entry_2_freshness"] == "fresh"
        assert info["entry_2_last_success_age_seconds"] == 11
        assert info["entry_3_freshness"] == "never_read"
        assert info["entry_3_last_success_age_seconds"] is None
        assert info["entry_1_poll_interval_seconds"] == 15
        assert info["entry_2_poll_interval_seconds"] == 30

        with patch.object(
            UwtClient,
            "read_state",
            AsyncMock(
                side_effect=CannotConnect(PRIVATE_REASON, reason="partial_envelope")
            ),
        ):
            await coordinators[0].async_refresh()
            await coordinators[2].async_refresh()
        failed = await system_health_info(hass)
        assert failed["available_entries"] == 1
        assert failed["entry_1_freshness"] == "unavailable"
        assert failed["entry_1_last_success_age_seconds"] == 31
        assert failed["entry_3_freshness"] == "never_read"
        assert failed["entry_3_last_success_age_seconds"] is None
        assert failed["entry_1_last_failure_category"] == "partial_envelope"
        assert failed["entry_3_last_failure_category"] == "partial_envelope"

        clock.now.return_value = NOW + timedelta(seconds=40)
        await coordinators[0].async_refresh()
        recovered = await system_health_info(hass)
        assert recovered["available_entries"] == 2
        assert recovered["entry_1_freshness"] == "fresh"
        assert recovered["entry_1_last_success_age_seconds"] == 0
        # This field describes the last failure, not an assertion of current failure.
        assert recovered["entry_1_last_failure_category"] == "partial_envelope"
        assert recovered["entry_2_last_success_age_seconds"] == 20


@pytest.mark.parametrize(
    ("state", "freshness"),
    [
        (ConfigEntryState.NOT_LOADED, "unloaded"),
        (ConfigEntryState.SETUP_RETRY, "never_read"),
        (ConfigEntryState.SETUP_ERROR, "never_read"),
        (ConfigEntryState.LOADED, "never_read"),
    ],
)
async def test_missing_runtime_is_not_a_success(
    hass: HomeAssistant, state: ConfigEntryState, freshness: str
) -> None:
    """Missing runtime data is valid during setup and after unloading."""
    _entry(hass, state=state)
    info = await system_health_info(hass)
    assert info["loaded_entries"] == int(state is ConfigEntryState.LOADED)
    assert info["available_entries"] == 0
    assert info["entry_1_freshness"] == freshness
    assert info["entry_1_last_success_age_seconds"] is None
    assert info["entry_1_last_failure_category"] is None


@pytest.mark.parametrize(
    "interval", [None, True, 0, 14, 31, 15.0, 20.5, "20", [], {}, PRIVATE_REASON]
)
async def test_invalid_saved_interval_is_safe_without_mutation(
    hass: HomeAssistant, interval: object
) -> None:
    """Persisted malformed values are neither exposed nor written back."""
    entry = _entry(hass, state=ConfigEntryState.NOT_LOADED, interval=interval)
    before = dict(entry.options)
    coordinator = _coordinator(hass, entry)
    info = await system_health_info(hass)
    assert info["entry_1_poll_interval_seconds"] == 15
    assert coordinator.update_interval == timedelta(seconds=15)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["options"][CONF_POLL_INTERVAL] == 15
    assert dict(entry.options) == before


async def test_health_query_is_private_and_does_not_probe(
    hass: HomeAssistant,
) -> None:
    """Cached failures disclose no identifiers and health opens no connections."""
    entry = _entry(hass, state=ConfigEntryState.SETUP_RETRY)
    coordinator = _coordinator(hass, entry)
    error = CannotConnect(
        f"{HOST} {DEVICE_ID} {PRIVATE_TITLE} {PRIVATE_REASON}",
        reason="partial_envelope",
    )
    with patch.object(UwtClient, "read_state", AsyncMock(side_effect=error)):
        await coordinator.async_refresh()
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE).add_to_hass(hass)
    MockConfigEntry(domain="other_integration").add_to_hass(hass)

    with (
        patch("asyncio.open_connection", side_effect=AssertionError("TCP probe")),
        patch(
            "aiohttp.ClientSession._request", side_effect=AssertionError("HTTP probe")
        ),
        patch.object(
            UwtClient, "read_state", side_effect=AssertionError("device read")
        ),
        patch.object(
            coordinator, "async_refresh", side_effect=AssertionError("refresh")
        ),
        patch.object(
            coordinator,
            "async_request_refresh",
            side_effect=AssertionError("scheduled refresh"),
        ),
    ):
        first = await system_health_info(hass)
        second = await system_health_info(hass)

    assert first == second
    assert first["configured_entries"] == 1
    assert first["loaded_entries"] == 0
    assert first["available_entries"] == 0
    assert first["entry_1_last_failure_category"] == "partial_envelope"
    serialized = json.dumps(first)
    for private in (HOST, DEVICE_ID, PRIVATE_TITLE, PRIVATE_REASON, entry.entry_id):
        assert private not in serialized


@pytest.mark.parametrize("interval", [15, 20, 30])
async def test_poll_interval_drives_runtime_freshness_and_cached_reports(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, interval: int
) -> None:
    """Runtime scheduling, two-interval freshness, and reports share one policy."""
    entry = _entry(hass, interval=interval)
    coordinator = _coordinator(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=interval)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch("custom_components.uplus_reborn.coordinator.datetime") as clock,
    ):
        clock.now.return_value = NOW
        await coordinator.async_refresh()
        for age, expected in ((interval * 2, "fresh"), (interval * 2 + 1, "stale")):
            clock.now.return_value = NOW + timedelta(seconds=age)
            health = await system_health_info(hass)
            diagnostics = await async_get_config_entry_diagnostics(hass, entry)
            assert coordinator.health.freshness == expected
            assert health["entry_1_freshness"] == expected
            assert health["entry_1_poll_interval_seconds"] == interval
            assert diagnostics["health"]["freshness"] == expected
            assert diagnostics["options"][CONF_POLL_INTERVAL] == interval
        read_state.assert_awaited_once_with()
