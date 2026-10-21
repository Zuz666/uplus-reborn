"""End-to-end Home Assistant tests for U+ Reborn entities."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PORT,
    STATE_UNAVAILABLE,
    EntityCategory,
    Platform,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.uplus_reborn.coordinator import UplusRebornCoordinator
from custom_components.uplus_reborn.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    ControlResult,
    DeviceSnapshot,
    InvalidFrame,
    OutcomeUnknown,
    UnsafeState,
    UwtClient,
    default_intent,
    fabric_intent,
    update_intent,
)
from custom_components.uplus_reborn.system_health import system_health_info

from .conftest import DEVICE_ID, HOST, PORT


def _entry(*, controls: bool = False, device_id: str = DEVICE_ID) -> MockConfigEntry:
    """Build one local appliance entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="HWD120-B1558U",
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: device_id,
        },
        options={CONF_ENABLE_VERIFIED_CONTROLS: controls} if controls else {},
        unique_id=device_id,
    )


def _entity_id(
    registry: er.EntityRegistry, platform: str, unique_id_suffix: str
) -> str:
    """Resolve one entity by its stable protocol-derived unique ID."""
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{DEVICE_ID}_{unique_id_suffix}"
    )
    assert entity_id is not None
    return entity_id


def _state(hass: HomeAssistant, entity_id: str) -> str:
    """Return one registered entity state."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def test_setup_exposes_verified_state_without_controls(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Default setup exposes shared state but no write surface."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.uplus_reborn.protocol.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert _state(hass, _entity_id(registry, "binary_sensor", "power")) == "on"
    remote_control_id = _entity_id(registry, "binary_sensor", "remote_control")
    assert _state(hass, remote_control_id) == "on"
    remote_control_entry = registry.async_get(remote_control_id)
    assert remote_control_entry is not None
    assert remote_control_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert (
        _state(hass, _entity_id(registry, "binary_sensor", "local_control_lock"))
        == "off"
    )
    assert (
        _state(hass, _entity_id(registry, "binary_sensor", "upper_door_lock")) == "off"
    )
    assert (
        _state(hass, _entity_id(registry, "binary_sensor", "lower_door_lock")) == "on"
    )
    assert _state(hass, _entity_id(registry, "sensor", "upper_program")) == "none"
    assert _state(hass, _entity_id(registry, "sensor", "upper_phase")) == "standby"
    lower_program_id = _entity_id(registry, "sensor", "lower_program")
    assert _state(hass, lower_program_id) == "none"
    lower_program = hass.states.get(lower_program_id)
    assert lower_program is not None
    assert {"cotton", "dry", "outdoor_care"} < set(lower_program.attributes["options"])
    for key in ("upper_program_selection", "lower_program_selection"):
        assert (
            registry.async_get_entity_id("select", DOMAIN, f"{DEVICE_ID}_{key}") is None
        )
    assert _state(hass, _entity_id(registry, "sensor", "lower_phase")) == "standby"
    assert _state(hass, _entity_id(registry, "sensor", "upper_cycle_count")) == "41"
    assert _state(hass, _entity_id(registry, "sensor", "lower_cycle_count")) == "97"
    consumption_values = {
        "session_water": (
            "2",
            SensorDeviceClass.WATER,
            UnitOfVolume.LITERS,
        ),
        "cumulative_water": (
            "63568",
            SensorDeviceClass.WATER,
            UnitOfVolume.LITERS,
        ),
        "session_energy": (
            "1.0",
            SensorDeviceClass.ENERGY,
            UnitOfEnergy.KILO_WATT_HOUR,
        ),
        "cumulative_energy": (
            "75309.0",
            SensorDeviceClass.ENERGY,
            UnitOfEnergy.KILO_WATT_HOUR,
        ),
    }
    for key, (expected, device_class, unit) in consumption_values.items():
        entity_id = _entity_id(registry, "sensor", key)
        assert _state(hass, entity_id) == expected
        entity_entry = registry.async_get(entity_id)
        assert entity_entry is not None
        assert entity_entry.entity_category is EntityCategory.DIAGNOSTIC
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["device_class"] == device_class
        assert state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
        assert state.attributes["unit_of_measurement"] == unit

    assert not any(
        item.domain in ("button", "select", "switch")
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["config"][CONF_PORT] == PORT
    assert diagnostics["device"]["state"]["upper"]["cycle_count"] == 41
    assert DEVICE_ID not in repr(diagnostics)
    assert HOST not in repr(diagnostics)
    json.dumps(diagnostics)


async def test_mac_identity_registers_network_connection(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A DHCP-derived UWT ID also links the HA device to its network MAC."""
    device_id = "98AAFC000001"
    entry = _entry(device_id=device_id)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.uplus_reborn.protocol.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, device_id)})
    assert device is not None
    assert (dr.CONNECTION_NETWORK_MAC, "98:aa:fc:00:00:01") in device.connections


@pytest.mark.parametrize("drum", ["upper", "lower"])
async def test_buttons_consume_only_selected_drum_and_publish_confirmed_state(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    active_snapshot: DeviceSnapshot,
    cancelled_snapshot: DeviceSnapshot,
    drum: str,
) -> None:
    """A staged Start leaves the other drum draft intact and updates real state."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    other = "lower" if drum == "upper" else "upper"
    before = replace(
        standby_snapshot,
        **{other: replace(active_snapshot.upper, cycle_active=True)},
    )
    active = replace(
        before,
        **{drum: replace(active_snapshot.upper, cycle_active=True)},
    )
    cancelled = replace(before, **{drum: cancelled_snapshot.upper})
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=before)
        ) as read_state,
        patch.object(
            UwtClient,
            "start_program",
            AsyncMock(return_value=ControlResult(before, active)),
        ) as start,
        patch.object(
            UwtClient,
            "control_drum",
            AsyncMock(return_value=ControlResult(active, cancelled)),
        ) as cancel,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        start_id = _entity_id(registry, "button", f"{drum}_start")
        cancel_id = _entity_id(registry, "button", f"{drum}_cancel")
        program_id = _entity_id(registry, "select", f"{drum}_program_selection")
        assert _state(hass, start_id) == STATE_UNAVAILABLE
        assert _state(hass, cancel_id) == STATE_UNAVAILABLE
        entry.runtime_data.stage_program(other, "spin")
        other_draft = entry.runtime_data.draft(other)
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: program_id, "option": "spin"},
            blocking=True,
        )
        selected = entry.runtime_data.draft(drum)
        await hass.async_block_till_done()
        assert _state(hass, start_id) != STATE_UNAVAILABLE
        assert _state(hass, _entity_id(registry, "sensor", f"{drum}_program")) == "none"
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: start_id}, blocking=True
        )
        await hass.async_block_till_done()
        start.assert_awaited_once_with(selected, allow_live=True, expected=before)
        assert entry.runtime_data.draft(other) is other_draft
        assert _state(hass, program_id) == "none"
        assert (
            _state(hass, _entity_id(registry, "sensor", f"{drum}_phase")) == "spinning"
        )
        assert _state(hass, start_id) == STATE_UNAVAILABLE
        assert _state(hass, cancel_id) != STATE_UNAVAILABLE
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: cancel_id}, blocking=True
        )
        await hass.async_block_till_done()
        cancel.assert_awaited_once_with(
            drum, "cancel", allow_live=True, expected=active
        )
        assert (
            _state(hass, _entity_id(registry, "sensor", f"{drum}_phase")) == "standby"
        )
        assert entry.runtime_data.draft(other) is other_draft
        assert getattr(entry.runtime_data.data, other) == getattr(before, other)


@pytest.mark.parametrize("drum", ["upper", "lower"])
async def test_draft_settings_are_local_atomic_and_program_specific(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, drum: str
) -> None:
    """Invalid settings do not partially alter either draft or contact hardware."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as control,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        coordinator = entry.runtime_data
        other = "lower" if drum == "upper" else "upper"
        program = "baby_care" if drum == "upper" else "cotton"
        coordinator.stage_program(other, "spin")
        other_draft = coordinator.draft(other)
        program_id = _entity_id(registry, "select", f"{drum}_program_selection")
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: program_id, "option": program},
            blocking=True,
        )
        speed_id = _entity_id(registry, "select", f"{drum}_spin_speed")
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: speed_id, "option": "600"},
            blocking=True,
        )
        eco_id = _entity_id(registry, "switch", f"{drum}_eco")
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: eco_id}, blocking=True
        )
        draft = coordinator.draft(drum)
        assert draft.program == program
        assert draft.spin_rpm == 600
        assert "eco" in draft.enabled_functions
        with pytest.raises(ValueError):
            coordinator.stage_settings(drum, spin_rpm=601, rinses=2)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "select",
                "select_option",
                {ATTR_ENTITY_ID: program_id, "option": "upper_spin_400"},
                blocking=True,
            )
        assert coordinator.draft(drum) is draft
        assert coordinator.draft(other) is other_draft
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["staged_intents"][drum]["spin_rpm"] == 600
        assert diagnostics["staged_intents"][other]["program"] == "spin"
        assert diagnostics["control_mode"] == "source_derived_opt_in"
        assert _state(hass, _entity_id(registry, "sensor", f"{drum}_program")) == "none"
        coordinator.stage_program(drum, "spin")
        assert coordinator.draft(drum) == default_intent(drum, "spin")
        coordinator.stage_program(drum, "none")
        assert coordinator.draft(drum) is None
        with pytest.raises(UnsafeState):
            await coordinator.async_start_program(drum)
        assert coordinator.health.latest_command.outcome == "not_dispatched"
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()
        control.assert_not_awaited()
        appliance.assert_not_awaited()


@pytest.mark.parametrize(
    ("drum", "program"),
    [
        ("upper", "kids_wear"),
        ("upper", "casual"),
        ("lower", "cotton"),
        ("lower", "mix"),
        ("lower", "synthetic"),
    ],
)
async def test_ordinary_mix_offers_stain_without_a_smart_recipe(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, drum: str, program: str
) -> None:
    """Supported ordinary programs offer panel stains without a smart fabric."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as control,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        coordinator = entry.runtime_data
        other = "lower" if drum == "upper" else "upper"
        coordinator.stage_program(other, "spin")
        other_draft = coordinator.draft(other)
        await hass.services.async_call(
            "select",
            "select_option",
            {
                ATTR_ENTITY_ID: _entity_id(
                    registry, "select", f"{drum}_program_selection"
                ),
                "option": program,
            },
            blocking=True,
        )
        original = coordinator.draft(drum)
        stain_id = _entity_id(registry, "select", f"{drum}_stain")
        stain = hass.states.get(stain_id)
        assert stain is not None
        assert stain.state == "none"
        assert stain.attributes["options"] == [
            "none",
            "blood",
            "grass",
            "wine",
            "juice",
            "soil",
            "milk",
        ]
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: stain_id, "option": "blood"},
            blocking=True,
        )
        assert _state(hass, stain_id) == "blood"
        assert (
            _state(hass, _entity_id(registry, "select", f"{drum}_program_selection"))
            == program
        )
        assert _state(hass, _entity_id(registry, "select", f"{drum}_fabric")) == "none"
        assert _state(hass, _entity_id(registry, "sensor", f"{drum}_program")) == "none"
        assert original.stains == ()
        assert coordinator.draft(drum) is not original
        assert coordinator.draft(drum).stains == ("blood",)
        assert coordinator.draft(other) is other_draft
        assert coordinator.data is standby_snapshot
        for setting in ("temperature", "wash_minutes"):
            assert (
                _state(hass, _entity_id(registry, "select", f"{drum}_{setting}"))
                == STATE_UNAVAILABLE
            )
        for setting in ("rinses", "spin_speed"):
            assert (
                _state(hass, _entity_id(registry, "select", f"{drum}_{setting}"))
                != STATE_UNAVAILABLE
            )
        await hass.services.async_call(
            "select",
            "select_option",
            {
                ATTR_ENTITY_ID: _entity_id(
                    registry, "select", f"{drum}_program_selection"
                ),
                "option": "spin",
            },
            blocking=True,
        )
        assert _state(hass, stain_id) == STATE_UNAVAILABLE
        assert coordinator.draft(drum).stains == ()
        assert coordinator.draft(other) is other_draft
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()
        control.assert_not_awaited()
        appliance.assert_not_awaited()


async def test_reload_discards_both_ephemeral_drafts(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Reload never restores a selected program into an executable intent."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ) as read_state:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            entry.runtime_data.stage_program(drum, "spin")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    for drum in ("upper", "lower"):
        assert entry.runtime_data.draft(drum) is None
        assert (
            _state(hass, _entity_id(registry, "select", f"{drum}_program_selection"))
            == "none"
        )
    assert read_state.await_count == 2


async def test_poll_failure_marks_entities_unavailable(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A failed live refresh must not leave stale state marked available."""
    entry = _entry()
    entry.add_to_hass(hass)
    read_state = AsyncMock(
        side_effect=[standby_snapshot, CannotConnect("appliance offline")]
    )
    with patch(
        "custom_components.uplus_reborn.protocol.UwtClient.read_state", read_state
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        power_id = _entity_id(registry, "binary_sensor", "power")
        assert _state(hass, power_id) == "on"

        await entry.runtime_data.async_request_refresh()
        await hass.async_block_till_done()

    assert _state(hass, power_id) == STATE_UNAVAILABLE
    assert read_state.await_count == 2


@pytest.mark.parametrize("controls", [False, True])
@pytest.mark.parametrize("online", [False, True])
async def test_upgrade_removes_only_obsolete_selectors_even_when_opted_out_or_offline(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    controls: bool,
    online: bool,
) -> None:
    """The old UI is removed before polling, without recreating retained entities."""
    entry = _entry(controls=controls)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_keys = [
        f"{drum}_{setting}"
        for drum in ("upper", "lower")
        for setting in (
            "high_water",
            "easy_iron",
            "intensive",
            "eco",
            "soak",
            "night",
        )
    ]
    obsolete = [
        registry.async_get_or_create(
            "select", DOMAIN, f"{DEVICE_ID}_{key}", config_entry=entry
        )
        for key in legacy_keys
    ]
    retained = registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{DEVICE_ID}_upper_program_selection",
        config_entry=entry,
        suggested_object_id="my_upper_program",
        original_name="Upper program selection",
        capabilities={"options": ["none", "spin", "towels"]},
    )
    retained = registry.async_update_entity(
        retained.entity_id, name="My preset", icon="mdi:tune"
    )
    # Seed persisted state, not a live entity that would collide during setup.
    # A legacy restore-cache value must never become executable intent.
    mock_restore_cache(hass, [State(retained.entity_id, "spin")])
    read_state = AsyncMock(
        return_value=standby_snapshot,
        side_effect=None if online else CannotConnect("offline"),
    )
    with (
        patch.object(UwtClient, "read_state", read_state),
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is online
        await hass.async_block_till_done()
        for removed in obsolete:
            assert registry.async_get(removed.entity_id) is None
        current = registry.async_get(retained.entity_id)
        assert current is not None
        assert current.id == retained.id
        assert current.unique_id == retained.unique_id
        assert current.entity_id == retained.entity_id
        assert current.name == "My preset"
        assert current.icon == "mdi:tune"
        assert entry.runtime_data.draft("upper") is None
        if online and controls:
            assert _state(hass, retained.entity_id) == "none"
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert registry.async_get(retained.entity_id).id == retained.id
            assert all(registry.async_get(item.entity_id) is None for item in obsolete)
        start.assert_not_awaited()
        cancel.assert_not_awaited()


@pytest.mark.parametrize(
    ("entity_domain", "platform", "owner", "unique_id"),
    [
        ("select", DOMAIN, "other", f"{DEVICE_ID}_upper_eco"),
        ("select", DOMAIN, "none", f"{DEVICE_ID}_upper_eco"),
        ("select", "other_integration", "this", f"{DEVICE_ID}_upper_eco"),
        ("sensor", DOMAIN, "this", f"{DEVICE_ID}_upper_eco"),
        ("select", DOMAIN, "this", f"{DEVICE_ID}_upper_unknown_setting"),
        ("select", DOMAIN, "this", "OTHERDEVICE_upper_eco"),
        ("select", DOMAIN, "this", f"{DEVICE_ID}_lower_spin_speed"),
        ("select", DOMAIN, "this", f"{DEVICE_ID}_lower_program_selection"),
    ],
)
async def test_upgrade_never_removes_foreign_or_unknown_registrations(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    entity_domain: str,
    platform: str,
    owner: str,
    unique_id: str,
) -> None:
    """Cleanup needs the exact entry, platform, entity domain, and known identity."""
    entry = _entry()
    entry.add_to_hass(hass)
    other = _entry(device_id="OTHERDEVICE")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    protected = registry.async_get_or_create(
        entity_domain,
        platform,
        unique_id,
        config_entry=entry if owner == "this" else other if owner == "other" else None,
    )
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(protected.entity_id) == protected


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("start_program", ("upper",)),
        ("start_program", ("lower",)),
        ("control_drum", ("upper", "pause")),
        ("control_drum", ("lower", "cancel")),
        ("set_appliance_option", ("child_lock", True)),
        ("set_appliance_option", ("power", False)),
    ],
)
async def test_queued_control_rechecks_opt_in_after_serialized_poll(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, kind: str, args: tuple
) -> None:
    """Opting out while a command waits for polling must prevent dispatch."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    client = UwtClient(HOST, DEVICE_ID, port=PORT)
    coordinator = UplusRebornCoordinator(hass, entry, client)
    coordinator.async_set_updated_data(standby_snapshot)
    coordinator.stage_program("upper", "spin")
    coordinator.stage_program("lower", "cotton")
    polling = asyncio.Event()
    release_poll = asyncio.Event()

    async def read_state() -> DeviceSnapshot:
        polling.set()
        await release_poll.wait()
        return standby_snapshot

    with (
        patch.object(UwtClient, "read_state", AsyncMock(side_effect=read_state)),
        patch.object(UwtClient, kind, AsyncMock()) as control,
    ):
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.wait_for(polling.wait(), 1)
        command = asyncio.create_task(getattr(coordinator, f"async_{kind}")(*args))
        await asyncio.sleep(0)
        assert not command.done()
        hass.config_entries.async_update_entry(entry, options={})
        release_poll.set()
        await poll
        with pytest.raises(UnsafeState):
            await command
        control.assert_not_awaited()
    assert coordinator.health.latest_command.outcome == "not_dispatched"
    assert coordinator.health.latest_command.cause == "controls_disabled"
    assert coordinator.draft("upper").program == "spin"
    assert coordinator.draft("lower").program == "cotton"


@pytest.mark.parametrize("interval", [15, 20, 30])
async def test_polling_only_options_preserve_runtime_and_staged_intent(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, interval: int
) -> None:
    """Saving polling updates the coordinator, with no query, reload, or command."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "cotton")
        lower_draft = coordinator.draft("lower")
        registry = er.async_get(hass)
        program_id = _entity_id(registry, "select", "upper_program_selection")
        entity_ids = {
            item.id: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        before = coordinator.health.last_success

        with patch.object(
            hass.config_entries, "async_reload", AsyncMock()
        ) as reload_entry:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_ENABLE_VERIFIED_CONTROLS: True, CONF_POLL_INTERVAL: interval},
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.runtime_data is coordinator
        assert coordinator.update_interval == timedelta(seconds=interval)
        assert coordinator.health.last_success == before
        assert coordinator.draft("upper").program == "spin"
        assert coordinator.draft("lower") is lower_draft
        assert _state(hass, program_id) == "spin"
        assert entity_ids == {
            item.id: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        health = await system_health_info(hass)
        assert diagnostics["options"][CONF_POLL_INTERVAL] == interval
        assert health["entry_1_poll_interval_seconds"] == interval
        read_state.assert_awaited_once_with()
        reload_entry.assert_not_awaited()
        start.assert_not_awaited()
        cancel.assert_not_awaited()


async def test_control_option_toggles_clear_intent_and_retain_user_preferences(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Off/on reloads retain registry customization but cannot restore a preset."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    program = registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{DEVICE_ID}_upper_program_selection",
        config_entry=entry,
    )
    program = registry.async_update_entity(
        program.entity_id, name="My upper preset", hidden_by=er.RegistryEntryHider.USER
    )
    cancelled = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DEVICE_ID}_cancel_upper_spin",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "cotton")

        for reload_count, enabled in enumerate((False, True), start=1):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_ENABLE_VERIFIED_CONTROLS: enabled, CONF_POLL_INTERVAL: 30},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            # Clearing happens in the save path, not only after a reload finishes.
            assert coordinator.draft("upper") is None
            assert coordinator.draft("lower") is None
            if not enabled:
                assert not coordinator.controls_enabled
                with pytest.raises(UnsafeState):
                    await coordinator.async_start_program("upper")
                with pytest.raises(UnsafeState):
                    await coordinator.async_control_drum("upper", "cancel")
            await hass.async_block_till_done()
            assert reload_entry.await_count == reload_count
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data is not coordinator
            assert entry.runtime_data.draft("upper") is None
            assert entry.runtime_data.draft("lower") is None
            assert entry.runtime_data.update_interval == timedelta(seconds=30)
            current = registry.async_get(program.entity_id)
            assert current.id == program.id
            assert current.name == "My upper preset"
            assert current.hidden_by is er.RegistryEntryHider.USER
            current_cancel = registry.async_get(cancelled.entity_id)
            assert current_cancel.id == cancelled.id
            assert current_cancel.disabled_by is er.RegistryEntryDisabler.USER
            assert hass.states.get(cancelled.entity_id) is None
            coordinator = entry.runtime_data

        assert _state(hass, program.entity_id) == "none"
        assert read_state.await_count == 3  # Initial setup and two deliberate reloads.
        start.assert_not_awaited()
        cancel.assert_not_awaited()


@pytest.mark.parametrize("changed", [False, True])
async def test_loaded_reconfigure_has_one_reload_owner(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, changed: bool
) -> None:
    """Validation preserves identity; only changed routing reloads, exactly once."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "cotton")
        drafts = {drum: coordinator.draft(drum) for drum in ("upper", "lower")}
        registry = er.async_get(hass)
        identities = {
            item.id: (item.entity_id, item.unique_id, item.device_id)
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        options = dict(entry.options)
        host, port = ("192.0.2.11", 56801) if changed else (HOST, PORT)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: host, CONF_PORT: port}
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.state is ConfigEntryState.LOADED
        assert entry.unique_id == DEVICE_ID
        assert entry.data == {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_DEVICE_ID: DEVICE_ID,
        }
        assert dict(entry.options) == options
        assert entry.runtime_data.client.host == host
        assert entry.runtime_data.client.port == port
        assert identities == {
            item.id: (item.entity_id, item.unique_id, item.device_id)
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert reload_entry.await_count == int(changed)
        assert read_state.await_count == 2 + int(changed)
        for drum, draft in drafts.items():
            assert entry.runtime_data.draft(drum) == (None if changed else draft)
        start.assert_not_awaited()
        cancel.assert_not_awaited()


@pytest.mark.parametrize("enabled", [None, False, 1, "true", [], {}])
async def test_only_boolean_opt_in_authorizes_control(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, enabled: object
) -> None:
    """Malformed persisted options must not become truthy write authorization."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_ENABLE_VERIFIED_CONTROLS: enabled}
    )
    coordinator = UplusRebornCoordinator(hass, entry, UwtClient(HOST, DEVICE_ID))
    entry.runtime_data = coordinator
    coordinator.async_set_updated_data(standby_snapshot)
    coordinator.stage_program("upper", "spin")
    with (
        patch.object(UwtClient, "read_state", AsyncMock()) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert not coordinator.controls_enabled
        for drum in ("upper", "lower"):
            with pytest.raises(UnsafeState):
                await coordinator.async_start_program(drum)
            for action in ("pause", "resume", "cancel", "reset"):
                with pytest.raises(UnsafeState):
                    await coordinator.async_control_drum(drum, action)
        with pytest.raises(UnsafeState):
            await coordinator.async_set_appliance_option("buzzer", False)
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["options"][CONF_ENABLE_VERIFIED_CONTROLS] is False
        assert diagnostics["control_mode"] == "monitoring_only"
    read_state.assert_not_awaited()
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


@pytest.mark.parametrize("window", ["first_refresh", "platform_setup"])
@pytest.mark.parametrize("change", ["polling", "routing", "reconfigure"])
async def test_setup_keeps_configuration_updates_received_while_awaiting(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    window: str,
    change: str,
) -> None:
    """A completed setup must use changes saved during either setup await."""
    entry = _entry()
    entry.add_to_hass(hass)
    paused = asyncio.Event()
    release = asyncio.Event()
    update_seen = asyncio.Event()
    read_routes: list[tuple[str, int]] = []
    forward_entry_setups = hass.config_entries.async_forward_entry_setups

    async def read_state(client: UwtClient) -> DeviceSnapshot:
        read_routes.append((client.host, client.port))
        if window == "first_refresh" and len(read_routes) == 1:
            paused.set()
            await release.wait()
        return standby_snapshot

    async def forward_platforms(
        config_entry: MockConfigEntry, platforms: tuple[Platform, ...]
    ) -> None:
        if window == "platform_setup" and not paused.is_set():
            paused.set()
            await release.wait()
        await forward_entry_setups(config_entry, platforms)

    async def observe_update(_hass: HomeAssistant, _entry: MockConfigEntry) -> None:
        update_seen.set()

    with (
        patch.object(UwtClient, "read_state", autospec=True, side_effect=read_state),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", forward_platforms
        ),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
    ):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(paused.wait(), 5)
            assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            initial_coordinator = entry.runtime_data
            remove_observer = entry.add_update_listener(observe_update)
            try:
                if change == "polling":
                    result = await hass.config_entries.options.async_init(
                        entry.entry_id
                    )
                    result = await hass.config_entries.options.async_configure(
                        result["flow_id"],
                        {
                            CONF_ENABLE_VERIFIED_CONTROLS: False,
                            CONF_POLL_INTERVAL: 30,
                        },
                    )
                    assert result["type"] is FlowResultType.CREATE_ENTRY
                elif change == "reconfigure":
                    result = await hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={
                            "source": SOURCE_RECONFIGURE,
                            "entry_id": entry.entry_id,
                        },
                    )
                    result = await hass.config_entries.flow.async_configure(
                        result["flow_id"],
                        {CONF_HOST: "192.0.2.11", CONF_PORT: 56801},
                    )
                    assert result["type"] is FlowResultType.ABORT
                    assert result["reason"] == "reconfigure_successful"
                else:
                    # The public config-entry update path also used by rediscovery.
                    hass.config_entries.async_update_entry(
                        entry,
                        data={
                            **entry.data,
                            CONF_HOST: "192.0.2.11",
                            CONF_PORT: 56801,
                        },
                    )
                # Drain the actual update notification before letting setup finish.
                await asyncio.wait_for(update_seen.wait(), 5)
            finally:
                remove_observer()
        finally:
            release.set()
            assert await asyncio.wait_for(setup, 5)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        if change == "polling":
            diagnostics = await async_get_config_entry_diagnostics(hass, entry)
            health = await system_health_info(hass)
            assert entry.options[CONF_POLL_INTERVAL] == 30
            assert diagnostics["options"][CONF_POLL_INTERVAL] == 30
            assert health["entry_1_poll_interval_seconds"] == 30
            assert entry.runtime_data.update_interval == timedelta(seconds=30)
            assert entry.runtime_data is initial_coordinator
            assert read_routes == [(HOST, PORT)]
            reload_entry.assert_not_awaited()
        else:
            assert entry.data[CONF_HOST] == "192.0.2.11"
            assert entry.data[CONF_PORT] == 56801
            assert entry.runtime_data.client.host == "192.0.2.11"
            assert entry.runtime_data.client.port == 56801
            assert read_routes[-1] == ("192.0.2.11", 56801)
            assert reload_entry.await_count <= 1
        assert entry.unique_id == DEVICE_ID
        assert entry.runtime_data.device_id == DEVICE_ID
        start.assert_not_awaited()
        cancel.assert_not_awaited()


@pytest.mark.parametrize("initial_enabled", [False, True])
async def test_setup_reconciles_opt_in_across_partial_control_platforms(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    initial_enabled: bool,
) -> None:
    """On and off/on saves cannot leave a selector-only or button-only surface."""
    entry = _entry(controls=initial_enabled)
    entry.add_to_hass(hass)
    select_ready = asyncio.Event()
    continue_buttons = asyncio.Event()
    buttons_ready = asyncio.Event()
    finish_setup = asyncio.Event()
    update_seen = asyncio.Event()
    first_forward = True
    forward_entry_setups = hass.config_entries.async_forward_entry_setups

    async def forward_platforms(
        config_entry: MockConfigEntry, platforms: tuple[Platform, ...]
    ) -> None:
        nonlocal first_forward
        if not first_forward:
            await forward_entry_setups(config_entry, platforms)
            return
        first_forward = False
        await forward_entry_setups(config_entry, (Platform.SELECT,))
        select_ready.set()
        await continue_buttons.wait()
        await forward_entry_setups(config_entry, (Platform.BUTTON,))
        buttons_ready.set()
        await finish_setup.wait()
        await forward_entry_setups(
            config_entry,
            tuple(
                platform
                for platform in platforms
                if platform not in (Platform.SELECT, Platform.BUTTON)
            ),
        )

    async def observe_update(_hass: HomeAssistant, _entry: MockConfigEntry) -> None:
        update_seen.set()

    async def save_controls(enabled: bool) -> None:
        update_seen.clear()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_ENABLE_VERIFIED_CONTROLS: enabled, CONF_POLL_INTERVAL: 15},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await asyncio.wait_for(update_seen.wait(), 5)

    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", forward_platforms
        ),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
    ):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(select_ready.wait(), 5)
            assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            initial_coordinator = entry.runtime_data
            registry = er.async_get(hass)
            if initial_enabled:
                program_id = _entity_id(registry, "select", "upper_program_selection")
                await hass.services.async_call(
                    "select",
                    "select_option",
                    {ATTR_ENTITY_ID: program_id, "option": "spin"},
                    blocking=True,
                )
                assert initial_coordinator.draft("upper").program == "spin"
                initial_coordinator.stage_program("lower", "cotton")
            remove_observer = entry.add_update_listener(observe_update)
            try:
                await save_controls(not initial_enabled)
                assert initial_coordinator.draft("upper") is None
                assert initial_coordinator.draft("lower") is None
                continue_buttons.set()
                await asyncio.wait_for(buttons_ready.wait(), 5)
                if initial_enabled:
                    await save_controls(True)
                    assert initial_coordinator.draft("upper") is None
                    assert initial_coordinator.draft("lower") is None
            finally:
                remove_observer()
        finally:
            continue_buttons.set()
            finish_setup.set()
            assert await asyncio.wait_for(setup, 5)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.controls_enabled
        for drum in ("upper", "lower"):
            assert entry.runtime_data.draft(drum) is None
            program_id = _entity_id(registry, "select", f"{drum}_program_selection")
            assert _state(hass, program_id) == "none"
            for action in ("start", "pause", "resume", "cancel", "reset"):
                button_id = _entity_id(registry, "button", f"{drum}_{action}")
                assert hass.states.get(button_id) is not None
            assert (
                hass.states.get(_entity_id(registry, "switch", f"{drum}_eco"))
                is not None
            )
        reload_entry.assert_awaited_once_with(entry.entry_id)
        assert read_state.await_count == 2
        start.assert_not_awaited()
        cancel.assert_not_awaited()


@pytest.mark.parametrize("drum", ["upper", "lower"])
@pytest.mark.parametrize("selection", ["program", "fabric", "ordinary_stain"])
async def test_atomic_action_keeps_drafts_and_serializes_with_polling(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    active_snapshot: DeviceSnapshot,
    drum: str,
    selection: str,
) -> None:
    """An automation submits its own immutable settings, not a concurrent UI draft."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    if selection == "fabric":
        settings = {"fabric": "B1", "stains": ["W30", "W1"]}
        expected = fabric_intent(drum, "B1", ("W1", "W30"))
        program_value = 0x15 if drum == "upper" else 0x02
        started = replace(
            active_snapshot.upper,
            program=expected.program,
            program_value=program_value,
            program_code="30500l" if drum == "upper" else "305002",
            phase="washing",
            phase_value=0x05,
            phase_code="305005",
            cycle_active=True,
        )
    elif selection == "ordinary_stain":
        program = "casual" if drum == "upper" else "mix"
        settings = {"program": program, "stains": ["blood"], "spin_rpm": 600}
        expected = update_intent(
            default_intent(drum, program), stains=("blood",), spin_rpm=600
        )
        started = replace(
            active_snapshot.upper,
            program=program,
            program_value=0x15 if drum == "upper" else 0x05,
            program_code="30500l" if drum == "upper" else "305005",
            phase="washing",
            phase_value=0x05,
            phase_code="305005",
            cycle_active=True,
        )
    else:
        settings = {"program": "spin", "spin_rpm": 600}
        expected = update_intent(default_intent(drum, "spin"), spin_rpm=600)
        started = replace(active_snapshot.upper, cycle_active=True)
    active = replace(standby_snapshot, **{drum: started})
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            UwtClient,
            "start_program",
            AsyncMock(return_value=ControlResult(standby_snapshot, active)),
        ) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "baby_care")
        coordinator.stage_program("lower", "cotton")
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DEVICE_ID)})
        polling = asyncio.Event()
        release = asyncio.Event()

        async def blocked_read() -> DeviceSnapshot:
            polling.set()
            await release.wait()
            return standby_snapshot

        read_state.side_effect = blocked_read
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.wait_for(polling.wait(), 1)
        command = asyncio.create_task(
            hass.services.async_call(
                DOMAIN,
                "start_program",
                {"device_id": device.id, "drum": drum, **settings},
                blocking=True,
            )
        )
        try:
            await asyncio.sleep(0)
            assert not command.done()
            start.assert_not_awaited()
            coordinator.stage_settings(drum, spin_rpm=400)
            drafts = {name: coordinator.draft(name) for name in ("upper", "lower")}
        finally:
            release.set()
        await poll
        await command
        start.assert_awaited_once_with(
            expected, allow_live=True, expected=standby_snapshot
        )
        for name, draft in drafts.items():
            assert coordinator.draft(name) is draft
        assert getattr(coordinator.data, drum) is started
        if selection == "fabric":
            submitted = start.await_args.args[0]
            assert submitted.stains == ("W1", "W30")
            assert (submitted.wash_minutes, submitted.temperature_c) == (40, 60)


@pytest.mark.parametrize(
    ("settings", "error_type"),
    [
        ({"program": "not_a_program"}, ServiceValidationError),
        ({"program": "spin", "spin_rpm": 601}, ServiceValidationError),
        ({"program": "spin", "delay_hours": 3}, ServiceValidationError),
        (
            {"program": "spin", "enabled_functions": ["high_water"]},
            ServiceValidationError,
        ),
        ({}, vol.Invalid),
        ({"program": "casual", "fabric": "B1"}, vol.Invalid),
        ({"program": "baby_care", "stains": ["W1"]}, ServiceValidationError),
        ({"program": "casual", "stains": ["blood", "grass"]}, ServiceValidationError),
        ({"program": "spin", "stains": ["blood"]}, ServiceValidationError),
        ({"program": "casual", "stains": ["blood", "W1"]}, ServiceValidationError),
        ({"fabric": "B1", "stains": ["blood"]}, ServiceValidationError),
        *[
            pytest.param(
                {"program": "casual", "stains": ["blood"], **override},
                ServiceValidationError,
                id=f"ordinary-stain-{name}",
            )
            for name, override in (
                ("temperature", {"temperature_c": 60}),
                ("wash-time", {"wash_minutes": 10}),
                ("eco", {"enabled_functions": ["eco"]}),
                ("night", {"enabled_functions": ["night"]}),
                ("soak", {"enabled_functions": ["soak"]}),
            )
        ],
        ({"fabric": "B1", "wash_minutes": 20}, ServiceValidationError),
        ({"fabric": "B1", "temperature_c": 30}, ServiceValidationError),
        ({"fabric": "B1", "rinses": 2}, ServiceValidationError),
        ({"fabric": "B1", "spin_rpm": 1000}, ServiceValidationError),
        ({"fabric": "B1", "delay_hours": 0}, ServiceValidationError),
        ({"fabric": "B1", "drying_mode": "off"}, ServiceValidationError),
        ({"fabric": "B1", "enabled_functions": []}, ServiceValidationError),
        *[
            pytest.param(
                {"program": "baby_care", field: value},
                vol.Invalid,
                id=f"{field}-boolean-{value}",
            )
            for field in (
                "wash_minutes",
                "temperature_c",
                "rinses",
                "spin_rpm",
                "delay_hours",
            )
            for value in (False, True)
        ],
    ],
)
async def test_atomic_action_rejects_invalid_settings_without_consuming_drafts(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    settings: dict,
    error_type: type[Exception],
) -> None:
    """Invalid automation input neither writes nor partially stages a program."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "baby_care")
        coordinator.stage_program("lower", "cotton")
        drafts = {drum: coordinator.draft(drum) for drum in ("upper", "lower")}
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DEVICE_ID)})
        with pytest.raises(error_type):
            await hass.services.async_call(
                DOMAIN,
                "start_program",
                {"device_id": device.id, "drum": "upper", **settings},
                blocking=True,
            )
        for drum, draft in drafts.items():
            assert coordinator.draft(drum) is draft
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()


async def test_atomic_action_targets_the_selected_appliance(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    active_snapshot: DeviceSnapshot,
) -> None:
    """A second loaded appliance cannot redirect an automation to the first."""
    first = _entry(controls=True)
    second = _entry(controls=True, device_id="OTHERDEVICE")
    first.add_to_hass(hass)
    active = replace(
        standby_snapshot,
        lower=replace(active_snapshot.upper, cycle_active=True),
    )
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)),
        patch.object(
            UwtClient,
            "start_program",
            AsyncMock(return_value=ControlResult(standby_snapshot, active)),
        ) as start,
    ):
        assert await hass.config_entries.async_setup(first.entry_id)
        second.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
        device = dr.async_get(hass).async_get_device(
            identifiers={(DOMAIN, "OTHERDEVICE")}
        )
        await hass.services.async_call(
            DOMAIN,
            "start_program",
            {"device_id": device.id, "drum": "lower", "program": "spin"},
            blocking=True,
        )
        registry = er.async_get(hass)
        first_phase = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DEVICE_ID}_lower_phase"
        )
        second_phase = registry.async_get_entity_id(
            "sensor", DOMAIN, "OTHERDEVICE_lower_phase"
        )
        assert _state(hass, first_phase) == "standby"
        assert _state(hass, second_phase) == "spinning"
        start.assert_awaited_once()


@pytest.mark.parametrize(
    "target", ["unknown", "foreign", "unloaded", "ambiguous", "opted_out"]
)
async def test_atomic_action_requires_one_loaded_opted_in_device(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, target: str
) -> None:
    """The registered global action cannot bypass entry ownership or opt-in."""
    entry = _entry(controls=target != "opted_out")
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        devices = dr.async_get(hass)
        device = devices.async_get_device(identifiers={(DOMAIN, DEVICE_ID)})
        if target == "foreign":
            other = MockConfigEntry(
                domain="other_integration", state=ConfigEntryState.LOADED
            )
            other.add_to_hass(hass)
            device = devices.async_get_or_create(
                config_entry_id=other.entry_id,
                identifiers={("other_integration", "foreign-device")},
            )
        if target == "unloaded":
            assert await hass.config_entries.async_unload(entry.entry_id)
        elif target == "ambiguous":
            other = MockConfigEntry(
                domain=DOMAIN, unique_id="OTHERDEVICE", state=ConfigEntryState.LOADED
            )
            other.add_to_hass(hass)
            devices.async_get_or_create(
                config_entry_id=other.entry_id, identifiers={(DOMAIN, DEVICE_ID)}
            )
        assert hass.services.has_service(DOMAIN, "start_program")
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "start_program",
                {
                    "device_id": "missing-device" if target == "unknown" else device.id,
                    "drum": "lower",
                    "program": "spin",
                },
                blocking=True,
            )
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()


@pytest.mark.parametrize("surface", ["button", "action"])
@pytest.mark.parametrize(
    ("failure", "translation_key", "outcome"),
    [
        (OutcomeUnknown("private"), "outcome_unknown", "unknown"),
        (CannotConnect("private"), "control_failed", "not_dispatched"),
        (InvalidFrame("private"), "control_failed", "not_dispatched"),
    ],
)
async def test_control_failure_invalidates_entities_until_authoritative_poll(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    active_snapshot: DeviceSnapshot,
    surface: str,
    failure: Exception,
    translation_key: str,
    outcome: str,
) -> None:
    """Control failure invalidates cached state without a blind retry or immediate query."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    recovered = replace(
        active_snapshot, upper=replace(active_snapshot.upper, cycle_active=True)
    )
    with (
        patch.object(
            UwtClient,
            "read_state",
            AsyncMock(
                side_effect=[
                    standby_snapshot,
                    CannotConnect("still unreachable"),
                    recovered,
                ]
            ),
        ) as read_state,
        patch.object(
            UwtClient, "start_program", AsyncMock(side_effect=failure)
        ) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "cotton")
        upper = coordinator.draft("upper")
        lower = coordinator.draft("lower")
        registry = er.async_get(hass)
        await hass.async_block_till_done()
        observed_ids = [
            _entity_id(registry, domain, key)
            for domain, key in (
                ("binary_sensor", "power"),
                ("sensor", "upper_phase"),
                ("sensor", "lower_phase"),
                ("select", "upper_program_selection"),
                ("select", "lower_program_selection"),
                ("button", "lower_start"),
                ("switch", "buzzer"),
            )
        ]
        assert all(
            _state(hass, entity_id) != STATE_UNAVAILABLE for entity_id in observed_ids
        )
        with pytest.raises(HomeAssistantError) as error:
            if surface == "button":
                await hass.services.async_call(
                    "button",
                    "press",
                    {ATTR_ENTITY_ID: _entity_id(registry, "button", "upper_start")},
                    blocking=True,
                )
            else:
                device = dr.async_get(hass).async_get_device(
                    identifiers={(DOMAIN, DEVICE_ID)}
                )
                await hass.services.async_call(
                    DOMAIN,
                    "start_program",
                    {"device_id": device.id, "drum": "upper", "program": "spin"},
                    blocking=True,
                )
        await hass.async_block_till_done()
        assert error.value.translation_key == translation_key
        assert "private" not in str(error.value)
        assert start.await_count == 1
        read_state.assert_awaited_once_with()
        assert coordinator.data is standby_snapshot
        assert coordinator.draft("upper") is (None if surface == "button" else upper)
        assert coordinator.draft("lower") is lower
        assert coordinator.health.latest_command.outcome == outcome
        assert not coordinator.health.latest_command.retried
        assert not coordinator.health.available
        assert all(
            _state(hass, entity_id) == STATE_UNAVAILABLE for entity_id in observed_ids
        )
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert not diagnostics["health"]["available"]
        assert diagnostics["device"]["state"]["upper"]["phase"] == "standby"

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert read_state.await_count == 2
        assert not coordinator.health.available
        assert all(
            _state(hass, entity_id) == STATE_UNAVAILABLE for entity_id in observed_ids
        )

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert read_state.await_count == 3
        assert coordinator.health.available
        assert _state(hass, _entity_id(registry, "sensor", "upper_phase")) == "spinning"
        assert all(
            _state(hass, entity_id) != STATE_UNAVAILABLE for entity_id in observed_ids
        )
        assert coordinator.data is recovered
        assert coordinator.health.latest_command.outcome == outcome
        assert not coordinator.health.latest_command.retried
        assert start.await_count == 1


async def test_appliance_switches_use_confirmed_state_and_preserve_drafts(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Appliance settings write once; power starts disabled and cycle drafts stay local."""
    before = replace(standby_snapshot, buzzer_enabled=True, child_lock=False)
    after = replace(before, buzzer_enabled=False)
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=before)),
        patch.object(
            UwtClient,
            "set_appliance_option",
            AsyncMock(return_value=ControlResult(before, after)),
        ) as control,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("lower", "cotton")
        draft = coordinator.draft("lower")
        registry = er.async_get(hass)
        power = registry.async_get(_entity_id(registry, "switch", "power"))
        assert power.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        buzzer_id = _entity_id(registry, "switch", "buzzer")
        assert registry.async_get(buzzer_id).entity_category is EntityCategory.CONFIG
        assert (
            registry.async_get(
                _entity_id(registry, "switch", "child_lock")
            ).entity_category
            is EntityCategory.CONFIG
        )
        assert _state(hass, buzzer_id) == "on"
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: buzzer_id}, blocking=True
        )
        await hass.async_block_till_done()
        control.assert_awaited_once_with(
            "buzzer", False, allow_live=True, expected=before
        )
        assert _state(hass, buzzer_id) == "off"
        assert coordinator.draft("lower") is draft


@pytest.mark.parametrize("drum", ["upper", "lower"])
async def test_lifecycle_buttons_follow_pause_resume_and_finished_state(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    active_snapshot: DeviceSnapshot,
    drum: str,
) -> None:
    """Native availability separates paused, active and finished, not just lock state."""
    active = replace(
        standby_snapshot, **{drum: replace(active_snapshot.upper, cycle_active=True)}
    )
    paused = replace(
        active, **{drum: replace(getattr(active, drum), cycle_active=False)}
    )
    finished = replace(
        active,
        **{
            drum: replace(
                getattr(active, drum),
                phase="completed",
                phase_code="30500j",
                phase_value=0x13,
                cycle_active=False,
                remaining_minutes=0,
            )
        },
    )
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=active)),
        patch.object(
            UwtClient,
            "control_drum",
            AsyncMock(
                side_effect=[
                    ControlResult(active, paused),
                    ControlResult(paused, active),
                    ControlResult(finished, standby_snapshot),
                ]
            ),
        ) as control,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        pause_id = _entity_id(registry, "button", f"{drum}_pause")
        resume_id = _entity_id(registry, "button", f"{drum}_resume")
        reset_id = _entity_id(registry, "button", f"{drum}_reset")
        assert _state(hass, pause_id) != STATE_UNAVAILABLE
        assert _state(hass, resume_id) == STATE_UNAVAILABLE
        assert _state(hass, reset_id) == STATE_UNAVAILABLE
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: pause_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert _state(hass, pause_id) == STATE_UNAVAILABLE
        assert _state(hass, resume_id) != STATE_UNAVAILABLE
        assert (
            _state(hass, _entity_id(registry, "binary_sensor", f"{drum}_door_lock"))
            == "on"
        )
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: resume_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert _state(hass, pause_id) != STATE_UNAVAILABLE
        assert _state(hass, resume_id) == STATE_UNAVAILABLE
        entry.runtime_data.async_set_updated_data(finished)
        await hass.async_block_till_done()
        assert _state(hass, pause_id) == STATE_UNAVAILABLE
        assert _state(hass, resume_id) == STATE_UNAVAILABLE
        assert _state(hass, reset_id) != STATE_UNAVAILABLE
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: reset_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert (
            _state(hass, _entity_id(registry, "sensor", f"{drum}_phase")) == "standby"
        )
        assert [call.args for call in control.await_args_list] == [
            (drum, "pause"),
            (drum, "resume"),
            (drum, "reset"),
        ]


@pytest.mark.parametrize("drum", ["upper", "lower"])
async def test_fabric_and_stain_selectors_keep_smart_drafts_local_and_fixed(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, drum: str
) -> None:
    """Smart choices replace only one draft and cannot expose manual recipe edits."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as control,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        coordinator = entry.runtime_data
        other = "lower" if drum == "upper" else "upper"
        coordinator.stage_program(other, "spin")
        other_draft = coordinator.draft(other)
        fabric_id = _entity_id(registry, "select", f"{drum}_fabric")
        stain_id = _entity_id(registry, "select", f"{drum}_stain")
        assert _state(hass, stain_id) == STATE_UNAVAILABLE
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: fabric_id, "option": "b1"},
            blocking=True,
        )
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: stain_id, "option": "w30"},
            blocking=True,
        )
        await hass.async_block_till_done()
        draft = coordinator.draft(drum)
        assert draft.fabric == "B1"
        assert draft.stains == ("W30",)
        assert (draft.wash_minutes, draft.temperature_c) == (40, 60)
        assert coordinator.draft(other) is other_draft
        assert _state(hass, fabric_id) == "b1"
        assert _state(hass, stain_id) == "w30"
        assert _state(hass, _entity_id(registry, "sensor", f"{drum}_program")) == "none"
        manual = ["wash_minutes", "temperature", "rinses", "spin_speed", "delay"]
        if drum == "lower":
            manual.append("drying")
        for setting in manual:
            assert (
                _state(hass, _entity_id(registry, "select", f"{drum}_{setting}"))
                == STATE_UNAVAILABLE
            )
        for option in ("high_water", "easy_iron", "intensive", "eco", "soak", "night"):
            assert (
                _state(hass, _entity_id(registry, "switch", f"{drum}_{option}"))
                == STATE_UNAVAILABLE
            )
        with pytest.raises(ValueError):
            coordinator.stage_settings(drum, spin_rpm=1000)
        assert coordinator.draft(drum) is draft
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: stain_id, "option": "none"},
            blocking=True,
        )
        assert _state(hass, fabric_id) == "b1"
        assert _state(hass, stain_id) == "none"
        assert coordinator.draft(drum) == fabric_intent(drum, "B1")
        await hass.services.async_call(
            "select",
            "select_option",
            {
                ATTR_ENTITY_ID: _entity_id(
                    registry, "select", f"{drum}_program_selection"
                ),
                "option": draft.program,
            },
            blocking=True,
        )
        await hass.async_block_till_done()
        assert coordinator.draft(drum).fabric is None
        assert coordinator.draft(drum).stains == ()
        assert _state(hass, fabric_id) == "none"
        assert _state(hass, stain_id) == "none"
        assert hass.states.get(stain_id).attributes["options"] == [
            "none",
            "blood",
            "grass",
            "wine",
            "juice",
            "soil",
            "milk",
        ]
        assert (
            _state(hass, _entity_id(registry, "select", f"{drum}_spin_speed"))
            != STATE_UNAVAILABLE
        )
        coordinator.stage_fabric(drum, "B1")
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: fabric_id, "option": "none"},
            blocking=True,
        )
        assert coordinator.draft(drum) is None
        assert coordinator.draft(other) is other_draft
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()
        control.assert_not_awaited()
        appliance.assert_not_awaited()


@pytest.mark.parametrize("conflict", ["eco", "night", "soak"])
async def test_ordinary_stain_clears_conflicts_and_restores_only_recipe_defaults(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, conflict: str
) -> None:
    """Stain transitions clear conflicts locally and retain independent settings."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as control,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        other_draft = coordinator.draft("upper")
        coordinator.stage_program("lower", "mix")
        coordinator.stage_settings("lower", spin_rpm=600, rinses=3)
        conflict_id = _entity_id(registry, "switch", f"lower_{conflict}")
        water_id = _entity_id(registry, "switch", "lower_high_water")
        for entity_id in (conflict_id, water_id):
            await hass.services.async_call(
                "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
        original = coordinator.draft("lower")
        assert set(original.enabled_functions) == {conflict, "high_water"}
        stain_id = _entity_id(registry, "select", "lower_stain")
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: stain_id, "option": "blood"},
            blocking=True,
        )
        treatment = coordinator.draft("lower")
        assert treatment is not original
        assert treatment.fabric is None
        assert treatment.stains == ("blood",)
        assert treatment.enabled_functions == ("high_water",)
        assert (treatment.spin_rpm, treatment.rinses) == (600, 3)
        assert set(original.enabled_functions) == {conflict, "high_water"}
        stain_state = hass.states.get(stain_id)
        assert stain_state.attributes["next_run_temperature_c"] == 60
        assert stain_state.attributes["next_run_wash_stage_minutes"] == 49
        assert _state(hass, water_id) == "on"
        for name in ("eco", "night", "soak"):
            assert (
                _state(hass, _entity_id(registry, "switch", f"lower_{name}"))
                == STATE_UNAVAILABLE
            )
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: conflict_id}, blocking=True
        )
        with pytest.raises(ValueError):
            coordinator.stage_settings("lower", enabled_functions=(conflict,))
        assert coordinator.draft("lower") is treatment
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: stain_id, "option": "none"},
            blocking=True,
        )
        cleared = coordinator.draft("lower")
        defaults = default_intent("lower", "mix")
        assert cleared.stains == ()
        assert (cleared.temperature_c, cleared.wash_minutes) == (
            defaults.temperature_c,
            defaults.wash_minutes,
        )
        assert cleared.enabled_functions == ("high_water",)
        assert (cleared.spin_rpm, cleared.rinses) == (600, 3)
        assert coordinator.draft("upper") is other_draft
        assert coordinator.data is standby_snapshot
        assert _state(hass, conflict_id) == "off"
        cleared_state = hass.states.get(stain_id)
        assert "next_run_temperature_c" not in cleared_state.attributes
        assert "next_run_wash_stage_minutes" not in cleared_state.attributes
        for setting in ("temperature", "wash_minutes"):
            assert (
                _state(hass, _entity_id(registry, "select", f"lower_{setting}"))
                != STATE_UNAVAILABLE
            )
        read_state.assert_awaited_once_with()
        start.assert_not_awaited()
        control.assert_not_awaited()
        appliance.assert_not_awaited()
