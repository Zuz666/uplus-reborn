"""Execution state reflects reports, independently of phase and HA controls."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplus_reborn.const import CONF_DEVICE_ID, DOMAIN
from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    DeviceSnapshot,
    UwtClient,
)

from .conftest import DEVICE_ID, HOST, PORT


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="HWD120-B1558U",
        data={"host": HOST, "port": PORT, CONF_DEVICE_ID: DEVICE_ID},
        unique_id=DEVICE_ID,
    )


def _state(hass: HomeAssistant, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DEVICE_ID}_{suffix}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def test_external_pause_resume_retains_phase_and_independent_drum_state(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Panel pause/resume changes execution, not phase or the other drum."""
    entry = _entry()
    entry.add_to_hass(hass)
    running = replace(
        standby_snapshot,
        **{
            drum: replace(
                getattr(standby_snapshot, drum),
                phase="spinning",
                phase_value=0x09,
                cycle_active=True,
            )
            for drum in ("upper", "lower")
        },
    )
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=running)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        for upper, lower, upper_state, lower_state in (
            (True, True, "running", "running"),
            (False, True, "paused", "running"),
            (False, False, "paused", "paused"),
            (True, False, "running", "paused"),
            (True, True, "running", "running"),
        ):
            coordinator.async_set_updated_data(
                replace(
                    running,
                    upper=replace(running.upper, cycle_active=upper),
                    lower=replace(running.lower, cycle_active=lower),
                )
            )
            await hass.async_block_till_done()
            assert _state(hass, "upper_execution_state") == upper_state
            assert _state(hass, "lower_execution_state") == lower_state
            assert _state(hass, "upper_phase") == "spinning"
            assert _state(hass, "lower_phase") == "spinning"


@pytest.mark.parametrize(
    ("phase", "phase_value", "active", "delay", "expected"),
    [
        ("standby", 0x00, False, False, "standby"),
        ("standby", 0x00, True, False, STATE_UNKNOWN),
        ("standby", 0x00, False, True, "delayed"),
        ("standby", 0x00, True, True, "delayed"),
        ("delayed_start", 0x0F, True, True, "delayed"),
        ("delayed_start", 0x0F, False, True, "delayed"),
        ("delayed_start", 0x0F, True, False, STATE_UNKNOWN),
        ("delayed_start", 0x0F, False, False, STATE_UNKNOWN),
        ("completed", 0x13, True, False, "completed"),
        ("drying_finished", 0x12, False, False, "completed"),
        ("washing", 0x05, True, False, "running"),
        ("washing", 0x05, False, False, "paused"),
        ("drying", 0x0C, True, False, "running"),
        ("cooling", 0x0D, False, False, "paused"),
        ("intermittent_rotation", 0x0E, True, False, "running"),
        ("intermittent_rotation", 0x10, False, False, "paused"),
        ("unsupported", 0xFE, True, True, STATE_UNKNOWN),
    ],
)
async def test_execution_context_precedes_activity(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    phase: str,
    phase_value: int,
    active: bool,
    delay: bool,
    expected: str,
) -> None:
    """Idle/finished/delay/unknown never become an invented pause or completion."""
    snapshot = replace(
        standby_snapshot,
        **{
            drum: replace(
                getattr(standby_snapshot, drum),
                phase=phase,
                phase_value=phase_value,
                cycle_active=active,
                delay_active=delay,
            )
            for drum in ("upper", "lower")
        },
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            assert _state(hass, f"{drum}_execution_state") == expected
            assert _state(hass, f"{drum}_phase") == phase


async def test_execution_connection_loss_recovery_and_reload(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A lost report is unavailable, not Paused; recovery and reload use fresh state."""
    paused = replace(
        standby_snapshot,
        upper=replace(standby_snapshot.upper, phase="spinning", phase_value=0x09),
        lower=replace(standby_snapshot.lower, phase="washing", phase_value=0x05),
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(
        UwtClient,
        "read_state",
        AsyncMock(
            side_effect=[
                standby_snapshot,
                CannotConnect("offline"),
                paused,
                standby_snapshot,
            ]
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            assert _state(hass, f"{drum}_execution_state") == STATE_UNAVAILABLE
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            assert _state(hass, f"{drum}_execution_state") == "paused"
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            assert _state(hass, f"{drum}_execution_state") == "standby"
