"""Shared Home Assistant fixtures for U+ Reborn."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_socket
from homeassistant.core import HomeAssistant

from custom_components.uplus_reborn.protocol import DeviceSnapshot, DrumSnapshot

DEVICE_ID = "TESTDEVICE123"
HOST = "192.0.2.10"
PORT = 56800


@pytest.fixture(autouse=True)
def _enable_custom_integrations(
    enable_custom_integrations: None, hass: HomeAssistant
) -> None:
    """Allow local integrations and satisfy declared Core dependencies."""
    hass.config.components.update(("dhcp", "network"))


@pytest.fixture
def loopback_socket(socket_enabled: None) -> None:
    """Allow synthetic loopback servers, never appliance or Internet connections."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


@pytest.fixture
def standby_snapshot() -> DeviceSnapshot:
    """Return the exact safe standby shape required by verified Start."""
    return DeviceSnapshot(
        observed_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        power=True,
        remote_control=True,
        local_lock=False,
        upper=DrumSnapshot(
            program_value=0,
            program_code="305000",
            program="none",
            phase_value=0,
            phase_code="305000",
            phase="standby",
            door_locked=False,
            remaining_minutes=None,
            cycle_count=41,
        ),
        lower=DrumSnapshot(
            program_value=0,
            program_code="305000",
            program="none",
            phase_value=0,
            phase_code="305000",
            phase="standby",
            door_locked=True,
            remaining_minutes=None,
            cycle_count=97,
        ),
        current_water_raw=2,
        total_water_raw=63568,
        current_electricity_raw=1,
        total_electricity_raw=75309,
    )


@pytest.fixture
def active_snapshot(standby_snapshot: DeviceSnapshot) -> DeviceSnapshot:
    """Return the exact verified active upper Spin state."""
    return DeviceSnapshot(
        observed_at=datetime(2026, 7, 27, 12, 0, 5, tzinfo=UTC),
        power=True,
        remote_control=True,
        local_lock=False,
        upper=DrumSnapshot(
            program_value=0x1C,
            program_code="30500s",
            program="spin",
            phase_value=0x0B,
            phase_code="30500b",
            phase="spinning",
            door_locked=True,
            remaining_minutes=1,
            cycle_count=standby_snapshot.upper.cycle_count,
        ),
        lower=standby_snapshot.lower,
        current_water_raw=standby_snapshot.current_water_raw,
        total_water_raw=standby_snapshot.total_water_raw,
        current_electricity_raw=standby_snapshot.current_electricity_raw,
        total_electricity_raw=standby_snapshot.total_electricity_raw,
    )


@pytest.fixture
def cancelled_snapshot(active_snapshot: DeviceSnapshot) -> DeviceSnapshot:
    """Return the verified safe state after upper Spin cancel."""
    return DeviceSnapshot(
        observed_at=datetime(2026, 7, 27, 12, 0, 12, tzinfo=UTC),
        power=True,
        remote_control=True,
        local_lock=False,
        upper=DrumSnapshot(
            program_value=0x1C,
            program_code="30500s",
            program="spin",
            phase_value=0,
            phase_code="305000",
            phase="standby",
            door_locked=False,
            remaining_minutes=None,
            cycle_count=active_snapshot.upper.cycle_count,
        ),
        lower=active_snapshot.lower,
        current_water_raw=active_snapshot.current_water_raw,
        total_water_raw=active_snapshot.total_water_raw,
        current_electricity_raw=active_snapshot.current_electricity_raw,
        total_electricity_raw=active_snapshot.total_electricity_raw,
    )


def capture_options_input(opened: dict, values: dict) -> dict:
    """Fill the native field names advertised by this options form."""
    if "diagnostics" not in values:
        return values
    schema = opened["data_schema"]
    if isinstance(schema, list):
        group = next(field for field in schema if field["name"] == "diagnostics")
        initial = group["default"]
        names = [field["name"] for field in group["schema"]]
    else:
        group = next(value for key, value in schema.schema.items() if key == "diagnostics")
        initial = next(key.default() for key in schema.schema if key == "diagnostics")
        names = [str(key) for key in group.schema.schema]
    field_names = {name.partition("__")[0]: name for name in names}
    return {
        **values,
        "diagnostics": {
            **initial,
            **{field_names[key]: value for key, value in values["diagnostics"].items()},
        },
    }
