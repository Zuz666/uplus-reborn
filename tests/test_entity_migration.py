"""Upgrade contracts for legacy entity storage, preferences, and Recorder identity."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder, history
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_HOST, CONF_PORT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    DOMAIN,
    MODEL,
)
from custom_components.uplus_reborn.protocol import DeviceSnapshot, UwtClient

from .conftest import DEVICE_ID, HOST, PORT

# Persisted pre-cutover identities, independent of the current entity descriptions.
_LEGACY_ENTITIES = (
    (
        ("binary_sensor", "power"),
        ("binary_sensor", "remote_control"),
        ("binary_sensor", "local_control_lock"),
        ("binary_sensor", "upper_door_lock"),
        ("binary_sensor", "lower_door_lock"),
        ("sensor", "upper_program"),
        ("sensor", "lower_program"),
        ("sensor", "upper_phase"),
        ("sensor", "lower_phase"),
        ("sensor", "upper_remaining_time"),
        ("sensor", "lower_remaining_time"),
        ("sensor", "upper_cycle_count"),
        ("sensor", "lower_cycle_count"),
        ("sensor", "session_water"),
        ("sensor", "cumulative_water"),
        ("sensor", "session_energy"),
        ("sensor", "cumulative_energy"),
        ("select", "upper_program_selection"),
        ("button", "start_upper_spin_400"),
        ("button", "cancel_upper_spin"),
    )
    + tuple(
        ("select", f"{drum}_{setting}")
        for drum in ("upper", "lower")
        for setting in ("wash_minutes", "temperature", "rinses", "spin_speed", "delay")
    )
    + (("select", "lower_program_selection"), ("select", "lower_drying"))
)
_OLD_DIAGNOSTICS = {
    "local_control_lock",
    "upper_cycle_count",
    "lower_cycle_count",
    "session_water",
    "session_energy",
}
_DIAGNOSTICS = {
    "power",
    "remote_control",
    "local_control_lock",
    "session_water",
    "session_energy",
    "upper_cycle_count",
    "lower_cycle_count",
    "cumulative_water",
    "cumulative_energy",
}
_CONSUMPTION = {
    "session_water": ("L", "water", "2"),
    "cumulative_water": ("L", "water", "63568"),
    "session_energy": ("kWh", "energy", "1.0"),
    "cumulative_energy": ("kWh", "energy", "75309.0"),
}


@pytest.fixture
async def mock_recorder_before_hass(async_test_recorder) -> None:
    """Initialize Recorder's database and patches before the hass fixture."""


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        unique_id=DEVICE_ID,
        data={CONF_HOST: HOST, CONF_PORT: PORT, CONF_DEVICE_ID: DEVICE_ID},
        options={CONF_ENABLE_VERIFIED_CONTROLS: True},
        pref_disable_new_entities=True,
        pref_disable_polling=False,
    )


def _legacy_storage(
    entry: MockConfigEntry, device_id: str, area_id: str, disabled_parity: int | None
) -> dict[str, Any]:
    """Build a sanitized copy of the old HA 1.20 entity-storage shape."""
    obsolete = [
        ("select", f"{drum}_{setting}")
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
    entities = []
    for index, (entity_domain, key) in enumerate((*_LEGACY_ENTITIES, *obsolete)):
        consumption = _CONSUMPTION.get(key)
        unit = (
            consumption[0]
            if consumption
            else "cycles"
            if key.endswith("cycle_count")
            else None
        )
        counter = consumption is not None or key.endswith("cycle_count")
        entities.append(
            {
                "aliases": [f"Laundry {key}"],
                "area_id": area_id,
                "capabilities": {"state_class": "total_increasing"} if counter else {},
                "categories": {},
                "config_entry_id": entry.entry_id,
                "config_subentry_id": None,
                "created_at": "2026-07-29T12:00:00+00:00",
                "device_class": None,
                "device_id": device_id,
                "disabled_by": "user" if index % 2 == disabled_parity else None,
                "entity_category": "diagnostic" if key in _OLD_DIAGNOSTICS else None,
                "entity_id": f"{entity_domain}.my_laundry_{key}",
                "has_entity_name": True,
                "hidden_by": "user" if index % 2 else None,
                "icon": "mdi:star",
                "id": f"legacy_registry_{index}",
                "labels": [],
                "modified_at": "2026-07-29T13:00:00+00:00",
                "name": f"My {key}",
                "object_id_base": key,
                "options": {"sensor": {"display_precision": 2}} if consumption else {},
                "original_device_class": consumption[1] if consumption else None,
                "original_icon": None,
                "original_name": key.replace("_", " "),
                "platform": DOMAIN,
                "previous_unique_id": None,
                "suggested_object_id": None,
                "supported_features": 0,
                "translation_key": key,
                "unique_id": f"{DEVICE_ID}_{key}",
                "unit_of_measurement": unit,
            }
        )
    return {
        "version": 1,
        "minor_version": 20,
        "data": {"entities": entities, "deleted_entities": []},
    }


@pytest.mark.parametrize("load_registries", [False])
@pytest.mark.parametrize("disabled_parity", [None, 0, 1])
async def test_legacy_storage_upgrade_preserves_preferences_and_category_semantics(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    standby_snapshot: DeviceSnapshot,
    disabled_parity: int | None,
) -> None:
    """Load old serialized data, not newly created entities with current defaults."""
    entry = _entry()
    entry.add_to_hass(hass)
    # load_registries=False keeps ER untouched until the legacy data is seeded.
    # The other registries used by the fixture still need their normal loading.
    await ar.async_load(hass)
    await dr.async_load(hass)
    area = ar.async_get(hass).async_create("Laundry")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, DEVICE_ID)}, name=MODEL
    )
    device = devices.async_update_device(
        device.id, area_id=area.id, name_by_user="My washer"
    )
    legacy = _legacy_storage(entry, device.id, area.id, disabled_parity)
    hass_storage[er.STORAGE_KEY] = deepcopy(legacy)
    await er.async_load(hass)
    registry = er.async_get(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)),
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Reload must preserve the same migration result and user preferences.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        start.assert_not_awaited()
        cancel.assert_not_awaited()
        appliance.assert_not_awaited()

    migrations = {
        "start_upper_spin_400": "upper_start",
        "cancel_upper_spin": "upper_cancel",
    }
    retained = {
        f"{DEVICE_ID}_{key}": migrations.get(key, key) for _, key in _LEGACY_ENTITIES
    }
    actual_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert {f"{DEVICE_ID}_{key}" for key in retained.values()} <= actual_ids
    assert not {f"{DEVICE_ID}_{key}" for key in migrations} & actual_ids
    preserved_fields = (
        "id",
        "entity_id",
        "platform",
        "config_entry_id",
        "device_id",
        "name",
        "icon",
        "area_id",
        "disabled_by",
        "hidden_by",
    )
    for old in legacy["data"]["entities"]:
        current = registry.async_get(old["entity_id"])
        if old["unique_id"] not in retained:
            assert current is None
            continue
        assert current is not None
        assert current.unique_id == f"{DEVICE_ID}_{retained[old['unique_id']]}"
        for field in preserved_fields:
            assert getattr(current, field) == old[field], (old["entity_id"], field)
        # Preserve explicit user choices; Core may add its own new defaults.
        for platform, user_options in old["options"].items():
            for option, value in user_options.items():
                assert current.options[platform][option] == value, (
                    old["entity_id"],
                    platform,
                    option,
                )
        assert set(current.aliases) == set(old["aliases"])
        assert current.created_at.isoformat() == old["created_at"]
        key = retained[old["unique_id"]]
        assert current.entity_category is (
            EntityCategory.DIAGNOSTIC if key in _DIAGNOSTICS else None
        )
        if key in _CONSUMPTION:
            unit, device_class, expected = _CONSUMPTION[key]
            assert current.unit_of_measurement == unit
            assert current.original_device_class == device_class
            assert (
                current.capabilities["state_class"] == SensorStateClass.TOTAL_INCREASING
            )
            if current.disabled_by is None:
                state = hass.states.get(current.entity_id)
                assert state is not None
                assert float(state.state) == float(expected)
                assert state.attributes["unit_of_measurement"] == unit
                assert (
                    state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
                )
        if current.disabled_by is not None:
            assert hass.states.get(current.entity_id) is None
    assert devices.async_get(device.id).name_by_user == "My washer"
    assert devices.async_get(device.id).area_id == area.id
    assert entry.pref_disable_new_entities
    assert not entry.pref_disable_polling


async def test_category_upgrade_keeps_old_and_new_states_in_same_recorder_history(
    hass: HomeAssistant, recorder_mock: Recorder, standby_snapshot: DeviceSnapshot
) -> None:
    """Category changes neither replace history identities nor rescale old values."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    keys = ("power", "remote_control", "local_control_lock", *_CONSUMPTION)
    previous = {}
    since = dt_util.utcnow() - timedelta(seconds=1)
    for key in keys:
        domain = "sensor" if key in _CONSUMPTION else "binary_sensor"
        unit = _CONSUMPTION[key][0] if key in _CONSUMPTION else None
        old = registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{DEVICE_ID}_{key}",
            config_entry=entry,
            suggested_object_id=f"my_history_{key}",
            entity_category=EntityCategory.DIAGNOSTIC
            if key.startswith("cumulative_")
            else None,
            unit_of_measurement=unit,
        )
        # These entities were enabled before the owner disabled new entities.
        registry.async_update_entity(old.entity_id, disabled_by=None)
        attributes = (
            {
                "unit_of_measurement": unit,
                "state_class": "total_increasing",
                "device_class": _CONSUMPTION[key][1],
            }
            if unit
            else {}
        )
        value = (
            "on"
            if key == "local_control_lock"
            else "off"
            if domain == "binary_sensor"
            else "1"
            if unit == "L"
            else "0.5"
        )
        hass.states.async_set(old.entity_id, value, attributes)
        previous[old.entity_id] = hass.states.get(old.entity_id)
    await hass.async_block_till_done()
    recorder_mock._async_commit(dt_util.utcnow())
    await hass.async_add_executor_job(recorder_mock.block_till_done)
    # These are old recorded states, not active entities occupying the IDs.
    for entity_id in previous:
        hass.states.async_remove(entity_id)
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    recorder_mock._async_commit(dt_util.utcnow())
    await hass.async_add_executor_job(recorder_mock.block_till_done)
    recorded = await hass.async_add_executor_job(
        partial(history.get_significant_states, hass, since, entity_ids=list(previous))
    )
    assert set(recorded) == set(previous)
    for entity_id, old in previous.items():
        assert old is not None
        assert any(
            item.state == old.state and item.last_updated == old.last_updated
            for item in recorded[entity_id]
        )
        current = hass.states.get(entity_id)
        assert current is not None
        assert recorded[entity_id][-1].state == current.state
        if entity_id.startswith("sensor."):
            assert (
                recorded[entity_id][0].attributes["unit_of_measurement"]
                == (current.attributes["unit_of_measurement"])
            )
            assert recorded[entity_id][0].attributes["state_class"] == (
                SensorStateClass.TOTAL_INCREASING
            )
