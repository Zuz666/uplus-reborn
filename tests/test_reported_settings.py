"""Authoritative reported settings remain separate from next-run controls."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    DOMAIN,
)
from custom_components.uplus_reborn.protocol import (
    UNKNOWN_VALUE,
    CannotConnect,
    DeviceSnapshot,
    ProgramSettingsReport,
    UwtClient,
    program_capabilities,
)

from .conftest import DEVICE_ID, HOST, PORT

_SCALARS = ("temperature", "wash_stage_time", "rinses", "spin_stage_time")
_FLAGS = (
    "high_water",
    "easy_iron",
    "intensive",
    "eco",
    "soak",
    "night",
    "smart_mode",
    "delay_active",
)
_REPORTED_ENTITIES = tuple(
    (platform, f"{drum}_reported_{setting}")
    for drum in ("upper", "lower")
    for platform, settings in (("sensor", _SCALARS), ("binary_sensor", _FLAGS))
    for setting in settings
) + (("sensor", "lower_reported_drying_mode"),)


def _entry(*, controls: bool = False) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="HWD120-B1558U",
        data={"host": HOST, "port": PORT, CONF_DEVICE_ID: DEVICE_ID},
        options={CONF_ENABLE_VERIFIED_CONTROLS: controls},
        unique_id=DEVICE_ID,
    )


def _entity_id(hass: HomeAssistant, platform: str, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{DEVICE_ID}_{suffix}"
    )
    assert entity_id is not None
    return entity_id


def _state(hass: HomeAssistant, platform: str, suffix: str) -> State:
    state = hass.states.get(_entity_id(hass, platform, suffix))
    assert state is not None
    return state


def _reported_states(hass: HomeAssistant) -> dict[str, str]:
    return {
        suffix: _state(hass, platform, suffix).state
        for platform, suffix in _REPORTED_ENTITIES
    }


@pytest.fixture
def reported_snapshot(standby_snapshot: DeviceSnapshot) -> DeviceSnapshot:
    """Two externally started cycles with deliberately different report values."""
    return replace(
        standby_snapshot,
        upper=replace(
            standby_snapshot.upper,
            program="casual",
            program_value=0x15,
            program_code="30500l",
            phase="washing",
            phase_value=0x05,
            phase_code="305005",
            cycle_active=True,
            reported_settings=ProgramSettingsReport(
                temperature_c=40,
                wash_minutes_encoded=47,
                rinses=3,
                spin_minutes=8,
                option_bits=0x25,
                smart=False,
                drying_value=None,
                stain_value=3,
            ),
        ),
        lower=replace(
            standby_snapshot.lower,
            program="cotton",
            program_value=0x01,
            program_code="305001",
            phase="washing",
            phase_value=0x05,
            phase_code="305005",
            cycle_active=True,
            delay_active=True,
            reported_settings=ProgramSettingsReport(
                temperature_c=0,
                wash_minutes_encoded=23,
                rinses=2,
                spin_minutes=5,
                option_bits=0x4A,
                smart=True,
                drying_value=7,
                stain_value=0,
            ),
        ),
    )


@pytest.mark.parametrize("controls", [False, True])
async def test_reported_entities_expose_each_drum_without_start(
    hass: HomeAssistant, reported_snapshot: DeviceSnapshot, controls: bool
) -> None:
    """Reports populate normal read-only entities without enabling or issuing controls."""
    entry = _entry(controls=controls)
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=reported_snapshot)),
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _reported_states(hass) == {
            "upper_reported_temperature": "40",
            "upper_reported_wash_stage_time": "47",
            "upper_reported_rinses": "3",
            "upper_reported_spin_stage_time": "8",
            "upper_reported_high_water": "on",
            "upper_reported_easy_iron": "off",
            "upper_reported_intensive": "on",
            "upper_reported_eco": "off",
            "upper_reported_soak": "on",
            "upper_reported_night": "off",
            "upper_reported_smart_mode": "off",
            "upper_reported_delay_active": "off",
            "lower_reported_temperature": "0",
            "lower_reported_wash_stage_time": "23",
            "lower_reported_rinses": "2",
            "lower_reported_spin_stage_time": "5",
            "lower_reported_high_water": "off",
            "lower_reported_easy_iron": "on",
            "lower_reported_intensive": "off",
            "lower_reported_eco": "on",
            "lower_reported_soak": "off",
            "lower_reported_night": "on",
            "lower_reported_smart_mode": "on",
            "lower_reported_delay_active": "on",
            "lower_reported_drying_mode": "30_min",
        }
        registry = er.async_get(hass)
        for platform, suffix in _REPORTED_ENTITIES:
            entity = registry.async_get(_entity_id(hass, platform, suffix))
            assert entity is not None
            assert entity.entity_category is None
            assert entity.disabled_by is None
            assert "state_class" not in _state(hass, platform, suffix).attributes
        for drum in ("upper", "lower"):
            assert _state(hass, "sensor", f"{drum}_reported_temperature").attributes[
                "unit_of_measurement"
            ] == UnitOfTemperature.CELSIUS
            for setting in ("wash_stage_time", "spin_stage_time"):
                assert _state(hass, "sensor", f"{drum}_reported_{setting}").attributes[
                    "unit_of_measurement"
                ] == UnitOfTime.MINUTES
            for unsupported in ("rpm", "delay", "original_wash_time", "stain"):
                assert registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{DEVICE_ID}_{drum}_reported_{unsupported}"
                ) is None
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DEVICE_ID}_upper_reported_drying_mode"
        ) is None
        start.assert_not_awaited()


async def test_external_reports_update_settings_without_consuming_drafts(
    hass: HomeAssistant, reported_snapshot: DeviceSnapshot
) -> None:
    """A decreasing stage counter and external options never become local intent."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=reported_snapshot)),
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        before = _reported_states(hass)
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "dry")
        drafts = {drum: coordinator.draft(drum) for drum in ("upper", "lower")}
        await hass.async_block_till_done()
        assert _reported_states(hass) == before
        upper = reported_snapshot.upper
        lower = reported_snapshot.lower
        changed = replace(
            reported_snapshot,
            upper=replace(
                upper,
                reported_settings=replace(
                    upper.reported_settings,
                    wash_minutes_encoded=19,
                    spin_minutes=4,
                    option_bits=0,
                    smart=True,
                ),
            ),
            lower=replace(
                lower,
                delay_active=False,
                reported_settings=replace(
                    lower.reported_settings, temperature_c=60, drying_value=10
                ),
            ),
        )
        coordinator.async_set_updated_data(changed)
        await hass.async_block_till_done()
        after = _reported_states(hass)
        assert after == {
            **before,
            "upper_reported_wash_stage_time": "19",
            "upper_reported_spin_stage_time": "4",
            "upper_reported_high_water": "off",
            "upper_reported_intensive": "off",
            "upper_reported_soak": "off",
            "upper_reported_smart_mode": "on",
            "lower_reported_temperature": "60",
            "lower_reported_delay_active": "off",
            "lower_reported_drying_mode": "auto",
        }
        for drum, draft in drafts.items():
            assert coordinator.draft(drum) is draft
        start.assert_not_awaited()


@pytest.mark.parametrize(
    ("phase_value", "phase", "cycle_active", "delay_active", "visible"),
    [
        (0x05, "washing", False, False, True),  # Paused, not absent.
        (0x0E, "intermittent_rotation", False, False, True),
        (0x0F, "delayed_start", False, True, True),
        (0x00, "standby", False, False, False),
        (0x00, "standby", True, False, True),
        (0x00, "standby", False, True, True),
        (0x13, "completed", False, False, False),
        (0x13, "completed", True, True, False),
        (0xFF, UNKNOWN_VALUE, True, True, False),
    ],
)
async def test_reported_settings_follow_authoritative_cycle_context(
    hass: HomeAssistant,
    reported_snapshot: DeviceSnapshot,
    phase_value: int,
    phase: str,
    cycle_active: bool,
    delay_active: bool,
    visible: bool,
) -> None:
    """Pause retains reports; retained coordinates alone cannot make an idle cycle."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=reported_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        expected = _reported_states(hass)
        changed = replace(
            reported_snapshot,
            **{
                drum: replace(
                    getattr(reported_snapshot, drum),
                    phase_value=phase_value,
                    phase=phase,
                    phase_code=None,
                    cycle_active=cycle_active,
                    delay_active=delay_active,
                )
                for drum in ("upper", "lower")
            },
        )
        entry.runtime_data.async_set_updated_data(changed)
        await hass.async_block_till_done()
        if visible:
            for drum in ("upper", "lower"):
                expected[f"{drum}_reported_delay_active"] = "on" if delay_active else "off"
        else:
            expected = dict.fromkeys(expected, STATE_UNKNOWN)
        assert _reported_states(hass) == expected


@pytest.mark.parametrize("missing", ["report", "unknown_program", "no_program"])
async def test_missing_reports_or_programs_do_not_invent_off_values(
    hass: HomeAssistant, reported_snapshot: DeviceSnapshot, missing: str
) -> None:
    """An active flag never fills absent or uninterpretable setting reports."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=reported_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        changes = (
            {"reported_settings": None}
            if missing == "report"
            else {
                "program": UNKNOWN_VALUE if missing == "unknown_program" else "none",
                "program_value": 0xFF if missing == "unknown_program" else 0,
                "program_code": None,
            }
        )
        entry.runtime_data.async_set_updated_data(
            replace(
                reported_snapshot,
                **{
                    drum: replace(getattr(reported_snapshot, drum), **changes)
                    for drum in ("upper", "lower")
                },
            )
        )
        await hass.async_block_till_done()
        assert _reported_states(hass) == dict.fromkeys(
            (suffix for _, suffix in _REPORTED_ENTITIES), STATE_UNKNOWN
        )


@pytest.mark.parametrize("drum", ["upper", "lower"])
@pytest.mark.parametrize(
    ("program", "applicable"),
    [
        ("drum_clean", _SCALARS),  # Fixed wash/rinse fields have empty allowed lists.
        ("rinse_spin", ("rinses", "spin_stage_time")),
        ("spin", ("spin_stage_time",)),
    ],
)
async def test_scalar_applicability_uses_program_semantics_not_editability(
    hass: HomeAssistant,
    reported_snapshot: DeviceSnapshot,
    drum: str,
    program: str,
    applicable: tuple[str, ...],
) -> None:
    """Fixed values remain visible; unrelated retained coordinates stay unknown."""
    actual = getattr(reported_snapshot, drum)
    changed = replace(
        reported_snapshot,
        **{
            drum: replace(
                actual,
                program=program,
                program_value=program_capabilities(drum, program).program_value,
                program_code=None,
                reported_settings=replace(actual.reported_settings, option_bits=0x6F),
            )
        },
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=changed)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        values = (40, 47, 3, 8) if drum == "upper" else (0, 23, 2, 5)
        for setting, value in zip(_SCALARS, values, strict=True):
            assert _state(hass, "sensor", f"{drum}_reported_{setting}").state == (
                str(value) if setting in applicable else STATE_UNKNOWN
            )
        # UI exclusions are not permission to discard an authoritative option bit.
        for flag in _FLAGS[:6]:
            assert _state(hass, "binary_sensor", f"{drum}_reported_{flag}").state == "on"


@pytest.mark.parametrize(
    ("program", "raw", "expected"),
    [
        ("cotton", 0, "off"),
        ("cotton", 10, "auto"),
        ("dry", 7, "30_min"),
        ("dry", 6, "60_min"),
        ("dry", 5, "90_min"),
        ("dry", 4, "120_min"),
        ("refresh", 7, "30_min"),
        ("dry", 255, STATE_UNKNOWN),
        ("dry", None, STATE_UNKNOWN),
        ("drum_clean", 7, STATE_UNKNOWN),
    ],
)
async def test_lower_drying_reports_only_known_applicable_modes(
    hass: HomeAssistant,
    reported_snapshot: DeviceSnapshot,
    program: str,
    raw: int | None,
    expected: str,
) -> None:
    """Drying is an actual mode, never a guessed duration or another scalar field."""
    lower = reported_snapshot.lower
    changed = replace(
        reported_snapshot,
        lower=replace(
            lower,
            program=program,
            program_value=program_capabilities("lower", program).program_value,
            program_code=None,
            reported_settings=replace(lower.reported_settings, drying_value=raw),
        ),
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=changed)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _state(hass, "sensor", "lower_reported_drying_mode").state == expected
        if program in ("dry", "refresh"):
            for scalar in _SCALARS:
                assert (
                    _state(hass, "sensor", f"lower_reported_{scalar}").state
                    == STATE_UNKNOWN
                )


@pytest.mark.parametrize(
    ("option_bits", "enabled"),
    [
        (0x01, "high_water"),
        (0x02, "easy_iron"),
        (0x04, "intensive"),
        (0x08, "eco"),
        (0x20, "soak"),
        (0x40, "night"),
        (0x10, None),
    ],
)
async def test_reported_options_preserve_independent_wire_bits(
    hass: HomeAssistant,
    reported_snapshot: DeviceSnapshot,
    option_bits: int,
    enabled: str | None,
) -> None:
    """Each named flag uses its own bit; the unmapped gap is not Soak."""
    changed = replace(
        reported_snapshot,
        **{
            drum: replace(
                getattr(reported_snapshot, drum),
                reported_settings=replace(
                    getattr(reported_snapshot, drum).reported_settings,
                    option_bits=option_bits,
                ),
            )
            for drum in ("upper", "lower")
        },
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=changed)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for drum in ("upper", "lower"):
            for flag in _FLAGS[:6]:
                assert _state(
                    hass, "binary_sensor", f"{drum}_reported_{flag}"
                ).state == ("on" if flag == enabled else "off")


async def test_settings_become_unavailable_then_recover_and_reload_from_reports(
    hass: HomeAssistant, reported_snapshot: DeviceSnapshot
) -> None:
    """Connection loss is not unknown; recovery/reload cannot revive cached settings."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    cleared = replace(
        reported_snapshot,
        upper=replace(reported_snapshot.upper, reported_settings=None),
        lower=replace(reported_snapshot.lower, reported_settings=None),
    )
    recovered = replace(
        reported_snapshot,
        lower=replace(
            reported_snapshot.lower,
            reported_settings=replace(
                reported_snapshot.lower.reported_settings, temperature_c=60
            ),
        ),
    )
    with patch.object(
        UwtClient,
        "read_state",
        AsyncMock(
            side_effect=[
                reported_snapshot,
                CannotConnect("offline"),
                recovered,
                cleared,
            ]
        ),
    ) as read_state:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_program("lower", "cotton")
        drafts = {drum: coordinator.draft(drum) for drum in ("upper", "lower")}
        expected = _reported_states(hass)
        registry = er.async_get(hass)
        identities = {
            item.unique_id: (item.id, item.entity_id, item.entity_category)
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        for suffix in (
            "upper_cycle_count",
            "lower_cycle_count",
            "cumulative_water",
            "cumulative_energy",
        ):
            assert identities[f"{DEVICE_ID}_{suffix}"][2] is EntityCategory.DIAGNOSTIC
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _reported_states(hass) == dict.fromkeys(
            (suffix for _, suffix in _REPORTED_ENTITIES), STATE_UNAVAILABLE
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        expected["lower_reported_temperature"] = "60"
        assert _reported_states(hass) == expected
        for drum, draft in drafts.items():
            assert coordinator.draft(drum) is draft
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        unknown = dict.fromkeys(
            (suffix for _, suffix in _REPORTED_ENTITIES), STATE_UNKNOWN
        )
        assert _reported_states(hass) == unknown
        assert entry.runtime_data.draft("upper") is None
        assert entry.runtime_data.draft("lower") is None
        assert identities == {
            item.unique_id: (item.id, item.entity_id, item.entity_category)
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert read_state.await_count == 4
