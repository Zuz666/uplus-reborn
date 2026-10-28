"""Tests for the U+ Reborn config flow."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_DHCP,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.uplus_reborn.cloud import (
    CloudDevice,
    CloudInvalidAuth,
    UPlusCloudClient,
)
from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    CONF_POLL_INTERVAL,
    DOMAIN,
    MODEL,
    SUPPORTED_WIFI_TYPE,
)
from custom_components.uplus_reborn.coordinator import UplusRebornCoordinator
from custom_components.uplus_reborn.discovery import DeviceNotFound
from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    DeviceSnapshot,
    InvalidFrame,
    ProtocolError,
    UwtClient,
)

from .conftest import DEVICE_ID, HOST, PORT, capture_options_input

_MANIFEST = json.loads(
    (
        Path(__file__).parents[1]
        / "custom_components"
        / "uplus_reborn"
        / "manifest.json"
    ).read_text(encoding="utf-8")
)
DHCP_MAC_PATTERN = _MANIFEST["dhcp"][0]["macaddress"]
DHCP_DEVICE_ID = f"{DHCP_MAC_PATTERN.removesuffix('*')}000001"
DHCP_MAC = DHCP_DEVICE_ID.lower()


def _dhcp_info(*, ip: str = HOST, hostname: str = "") -> DhcpServiceInfo:
    """Build one DHCP discovery event for the verified appliance OUI."""
    return DhcpServiceInfo(ip=ip, hostname=hostname, macaddress=DHCP_MAC)


def _entry(*, state: ConfigEntryState = ConfigEntryState.NOT_LOADED) -> MockConfigEntry:
    """Build one configured appliance entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: DEVICE_ID,
        },
        unique_id=DEVICE_ID,
        state=state,
    )


async def _start_user_step(hass: HomeAssistant, next_step_id: str) -> ConfigFlowResult:
    """Choose one setup route from the initial user menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": next_step_id}
    )


async def test_dhcp_flow_derives_connection_and_name(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """DHCP supplies the IP, UWT device ID, and an optional useful name."""
    with patch(
        "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=_dhcp_info(hostname="laundry-room"),
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"
    assert result["description_placeholders"] == {
        "host": HOST,
        "name": "laundry-room",
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "laundry-room"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: PORT,
        CONF_DEVICE_ID: DHCP_DEVICE_ID,
    }
    assert result["result"].unique_id == DHCP_DEVICE_ID


async def test_dhcp_flow_uses_model_without_hostname(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """The supported model is the deterministic fallback discovery title."""
    with patch(
        "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=_dhcp_info(),
        )

    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["name"] == MODEL


async def test_dhcp_flow_updates_existing_appliance_ip(
    hass: HomeAssistant,
) -> None:
    """A later DHCP event refreshes the connection without duplicating the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: DHCP_DEVICE_ID,
        },
        unique_id=DHCP_DEVICE_ID,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=_dhcp_info(ip="192.0.2.11"),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.11"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_flow_creates_one_validated_entry(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A live checksum-valid snapshot is required before entry creation."""
    result = await _start_user_step(hass, "manual")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    with patch(
        "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ) as read_state:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: f" {HOST} ",
                CONF_PORT: PORT,
                CONF_DEVICE_ID: f" {DEVICE_ID} ",
                "confirm_model": True,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MODEL
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: PORT,
        CONF_DEVICE_ID: DEVICE_ID,
    }
    assert read_state.await_count == 2


async def test_user_flow_reports_connection_failure(
    hass: HomeAssistant,
) -> None:
    """A failed probe keeps the form open and stores no entry."""
    result = await _start_user_step(hass, "manual")

    with patch(
        "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
        AsyncMock(side_effect=CannotConnect("offline")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: HOST,
                CONF_PORT: PORT,
                CONF_DEVICE_ID: DEVICE_ID,
                "confirm_model": True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_rejects_non_ascii_device_id(
    hass: HomeAssistant,
) -> None:
    """The wire identity must be ASCII before any connection is attempted."""
    result = await _start_user_step(hass, "manual")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: "invalid-é",
            "confirm_model": True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_device_id"}


async def test_user_flow_aborts_duplicate_after_live_validation(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """The device ID is the stable identity for duplicate prevention."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _start_user_step(hass, "manual")

    with patch(
        "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
        AsyncMock(return_value=standby_snapshot),
    ) as read_state:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.0.2.11",
                CONF_PORT: PORT,
                CONF_DEVICE_ID: DEVICE_ID,
                "confirm_model": True,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    read_state.assert_awaited_once()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_cloud_flow_discovers_identity_name_and_host(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Account discovery supplies identity/name and LAN discovery supplies host."""
    result = await _start_user_step(hass, "cloud")
    cloud_device = CloudDevice(
        device_id=DEVICE_ID,
        name="Laundry",
        online=True,
        wifi_type=SUPPORTED_WIFI_TYPE,
    )
    with patch.object(
        UPlusCloudClient,
        "async_discover_devices",
        AsyncMock(return_value=(cloud_device,)),
    ) as discover_devices:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: " owner@example.com ",
                CONF_PASSWORD: "secret",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_device"
    discover_devices.assert_awaited_once_with("owner@example.com", "secret")

    with (
        patch(
            "custom_components.uplus_reborn.config_flow.async_find_device_host",
            AsyncMock(return_value=HOST),
        ) as find_host,
        patch(
            "custom_components.uplus_reborn.config_flow.UwtClient.read_state",
            AsyncMock(return_value=standby_snapshot),
        ) as read_state,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: ""},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Laundry"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: PORT,
        CONF_DEVICE_ID: DEVICE_ID,
    }
    find_host.assert_awaited_once_with(DEVICE_ID)
    assert read_state.await_count == 2
    assert CONF_EMAIL not in result["data"]
    assert CONF_PASSWORD not in result["data"]


async def test_cloud_flow_allows_manual_host_when_lan_discovery_fails(
    hass: HomeAssistant,
) -> None:
    """A failed MAC lookup keeps identity/name and offers a host fallback."""
    result = await _start_user_step(hass, "cloud")
    cloud_device = CloudDevice(
        device_id=DEVICE_ID,
        name="Laundry",
        online=True,
        wifi_type=SUPPORTED_WIFI_TYPE,
    )
    with patch.object(
        UPlusCloudClient,
        "async_discover_devices",
        AsyncMock(return_value=(cloud_device,)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "secret"},
        )

    with patch(
        "custom_components.uplus_reborn.config_flow.async_find_device_host",
        AsyncMock(side_effect=DeviceNotFound),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: ""},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_device"
    assert result["errors"] == {CONF_HOST: "device_not_found"}


async def test_cloud_flow_reports_invalid_account_credentials(
    hass: HomeAssistant,
) -> None:
    """Rejected cloud credentials are reported without creating an entry."""
    result = await _start_user_step(hass, "cloud")
    with patch.object(
        UPlusCloudClient,
        "async_discover_devices",
        AsyncMock(side_effect=CloudInvalidAuth),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud"
    assert result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
    ],
)
async def test_reconfigure_updates_only_connection_settings(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, state: ConfigEntryState
) -> None:
    """Validated routing must also recover entries without an active listener."""
    entry = _entry(state=state)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM

    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.0.2.11", CONF_PORT: 56801},
        )
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once_with(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.client.host == "192.0.2.11"
        assert entry.runtime_data.client.port == 56801
        assert read_state.await_count == 2  # Validation and recovered setup.
        start.assert_not_awaited()
        cancel.assert_not_awaited()
        appliance.assert_not_awaited()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_HOST: "192.0.2.11",
        CONF_PORT: 56801,
        CONF_DEVICE_ID: DEVICE_ID,
    }
    assert entry.unique_id == DEVICE_ID


async def test_options_flow_requires_explicit_control_opt_in(
    hass: HomeAssistant,
) -> None:
    """Only final confirmation saves preferences and explicit control consent."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["last_step"] is False
    assert result["data_schema"]({})[CONF_ENABLE_VERIFIED_CONTROLS] is False

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENABLE_VERIFIED_CONTROLS: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert not entry.options
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_ENABLE_VERIFIED_CONTROLS: True,
        CONF_POLL_INTERVAL: 15,
        "diagnostic_operations": 1800,
        "diagnostic_events": 18000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 21600,
    }


@pytest.mark.parametrize(
    ("reading", "failed", "status"),
    [
        ("fresh", False, "connected"),
        ("fresh", True, "offline"),
        ("stale", False, "unknown"),
        ("never", False, "unknown"),
        ("never", True, "offline"),
        ("never", True, "offline_polling_disabled"),
    ],
)
async def test_options_show_cached_connection_without_probing(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    reading: str,
    failed: bool,
    status: str,
) -> None:
    """Opening options distinguishes fresh contact, failure and unknown state."""
    entry = _entry()
    entry.add_to_hass(hass)
    if status == "offline_polling_disabled":
        hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    client = UwtClient(host=HOST, port=PORT, device_id=DEVICE_ID)
    coordinator = UplusRebornCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    with patch.object(UwtClient, "read_state", AsyncMock()) as read_state:
        if reading != "never":
            read_state.return_value = standby_snapshot
            await coordinator.async_refresh()
            if reading == "stale":
                coordinator._last_success = datetime.now(UTC) - timedelta(minutes=2)
        if failed:
            read_state.side_effect = CannotConnect("offline")
            await coordinator.async_refresh()
        read_state.reset_mock()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({})["connection_status"] == status
        read_state.assert_not_awaited()


@pytest.mark.parametrize("interval", [15, 20, 30])
async def test_options_store_bounded_integer_seconds(
    hass: HomeAssistant, interval: int
) -> None:
    """The native flow persists only integration options, never routing."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock()) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_ENABLE_VERIFIED_CONTROLS: False, CONF_POLL_INTERVAL: interval},
        )
        assert result["step_id"] == "confirm"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {
        CONF_ENABLE_VERIFIED_CONTROLS: False,
        CONF_POLL_INTERVAL: interval,
        "diagnostic_operations": 1800,
        "diagnostic_events": 18000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 21600,
    }
    assert type(entry.options[CONF_POLL_INTERVAL]) is int
    assert entry.data[CONF_HOST] == HOST
    read_state.assert_not_awaited()
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


@pytest.mark.parametrize("interval", [None, True, False, 0, 14, 31, 15.0, 20.5, "20"])
async def test_options_reject_invalid_poll_interval(
    hass: HomeAssistant, interval: object
) -> None:
    """Out-of-range or non-integer input cannot change stored configuration."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch.object(UwtClient, "read_state", AsyncMock()) as read_state,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_ENABLE_VERIFIED_CONTROLS: True, CONF_POLL_INTERVAL: interval},
            )
        await hass.async_block_till_done()
    assert dict(entry.options) == {}
    read_state.assert_not_awaited()
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (CannotConnect("offline"), "cannot_connect"),
        (InvalidFrame("bad checksum", reason="checksum_mismatch"), "invalid_response"),
        (ProtocolError("unsupported"), "unknown"),
    ],
)
async def test_reconfigure_failure_preserves_identity_and_routing(
    hass: HomeAssistant, error: ProtocolError, message: str
) -> None:
    """A failed read never changes the saved address or schedules a reload."""
    entry = _entry()
    entry.add_to_hass(hass)
    before = dict(entry.data)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(side_effect=error)
        ) as read_state,
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.11", CONF_PORT: 56801}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": message}
    assert dict(entry.data) == before
    assert entry.unique_id == DEVICE_ID
    assert dict(entry.options) == {}
    read_state.assert_awaited_once_with()
    reload_entry.assert_not_awaited()
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


async def test_loaded_dhcp_update_uses_the_listener_reload_only(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Rediscovery must not combine the Core helper's reload with our listener."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: DHCP_DEVICE_ID,
        },
        unique_id=DHCP_DEVICE_ID,
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=_dhcp_info(ip="192.0.2.11"),
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == DHCP_DEVICE_ID
    assert entry.runtime_data.client.host == "192.0.2.11"
    assert entry.state is ConfigEntryState.LOADED
    reload_entry.assert_awaited_once_with(entry.entry_id)
    assert read_state.await_count == 2
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


async def test_reconfigure_preserves_explicit_entry_disablement(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """A validated route correction cannot silently enable a disabled entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        data={CONF_HOST: HOST, CONF_PORT: PORT, CONF_DEVICE_ID: DEVICE_ID},
        unique_id=DEVICE_ID,
        disabled_by=ConfigEntryDisabler.USER,
        options={CONF_POLL_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
        patch.object(UwtClient, "control_drum", AsyncMock()) as cancel,
        patch.object(UwtClient, "set_appliance_option", AsyncMock()) as appliance,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.11", CONF_PORT: 56801}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.unique_id == DEVICE_ID
    assert entry.data[CONF_HOST] == "192.0.2.11"
    assert dict(entry.options) == {CONF_POLL_INTERVAL: 30}
    reload_entry.assert_awaited_once_with(entry.entry_id)
    read_state.assert_awaited_once_with()  # Validation only; disabled setup stays off.
    start.assert_not_awaited()
    cancel.assert_not_awaited()
    appliance.assert_not_awaited()


@pytest.mark.parametrize(
    ("budgets", "interval", "polling_disabled", "expected_depth"),
    [
        ({}, 15, False, 27000),
        ({"diagnostic_operations": 2}, 15, False, 30),
        ({"diagnostic_events": 19}, 20, False, 20),
        ({"diagnostic_raw_bytes": 531}, 30, False, 30),
        ({"diagnostic_raw_bytes": 1}, 15, False, 0),
        ({
            "diagnostic_operations": 600,
            "diagnostic_events": 12000,
            "diagnostic_raw_bytes": 8388608,
            "diagnostic_duration": "7200",
        }, 30, False, 18000),
        ({}, 15, True, 0),
    ],
)
async def test_capture_confirmation_shows_exact_effective_budgets_and_depth(
    hass: HomeAssistant,
    budgets: dict[str, object],
    interval: int,
    polling_disabled: bool,
    expected_depth: int,
) -> None:
    """All three budgets constrain complete ordinary-poll retained capacity."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, pref_disable_polling=polling_disabled
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        capture_options_input(result, {
            CONF_POLL_INTERVAL: interval, "diagnostics": {**budgets, "start_capture": True},
        }),
    )
    assert result["step_id"] == "confirm"
    selected = {
        "diagnostic_operations": 1800,
        "diagnostic_events": 18000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": "21600",
        **budgets,
    }
    displayed = {
        name.partition("__")[0]: value for name, value in result["data_schema"]({}).items()
    }
    for key, value in selected.items():
        assert int(displayed[key].split(" → ")[-1]) == int(value)
    assert int(displayed["estimated_seconds"]) == expected_depth
    assert displayed["review_action"] == "start"
    assert displayed["capture_replacement"] == "replace"
    assert "start_capture" not in displayed
    assert not entry.options
    hass.config_entries.options.async_abort(result["flow_id"])
    assert not entry.options


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diagnostic_operations", 0),
        ("diagnostic_operations", 1801),
        ("diagnostic_events", 18001),
        ("diagnostic_events", 1.5),
        ("diagnostic_raw_bytes", 8388609),
        ("diagnostic_raw_bytes", 15.0),
        ("diagnostic_raw_bytes", "26.6"),
        ("diagnostic_raw_bytes", None),
        ("diagnostic_duration", "0"),
        ("diagnostic_duration", "3601"),
        ("diagnostic_duration", 3600),
        ("diagnostic_duration", True),
    ],
)
async def test_capture_options_reject_invalid_input_without_saving(
    hass: HomeAssistant, key: str, value: object
) -> None:
    """Native validation cannot persist unbounded or noninteger preferences."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    try:
        invalid = await hass.config_entries.options.async_configure(
            result["flow_id"], capture_options_input(result, {"diagnostics": {key: value}})
        )
    except InvalidData:
        pass
    else:
        assert invalid["step_id"] == "init"
        assert invalid["errors"] == {"base": "invalid_options"}
    assert not entry.options


@pytest.mark.parametrize(
    "key", ["diagnostic_operations", "diagnostic_events", "diagnostic_raw_bytes"]
)
async def test_capture_boolean_budgets_do_not_count_as_one(
    hass: HomeAssistant, key: str
) -> None:
    """Python's bool/int relationship does not turn consent into a budget."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], capture_options_input(result, {"diagnostics": {key: True}})
        )


async def test_capture_defaults_normalize_persisted_options_without_applying(
    hass: HomeAssistant,
) -> None:
    """An old or malformed entry shows independently safe effective defaults."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {
        "diagnostic_operations": 1801,
        "diagnostic_events": 37,
        "diagnostic_raw_bytes": True,
        "diagnostic_duration": 0,
    }
    hass.config_entries.async_update_entry(entry, options=saved)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    shown = {
        name.partition("__")[0]: value
        for name, value in result["data_schema"]({})["diagnostics"].items()
    }
    assert shown["diagnostic_operations"] == "1800"
    assert shown["diagnostic_events"] == "37"
    assert shown["diagnostic_raw_bytes"] == "4194304"
    assert shown["diagnostic_duration"] == "21600"
    hass.config_entries.options.async_abort(result["flow_id"])
    assert dict(entry.options) == saved


@pytest.mark.parametrize(
    ("operations", "events", "raw_bytes", "duration"),
    [
        (417, 8220, 750000, 7200),
        (300, 6000, 4194304, 3600),
        (600, 12000, 4194304, 7200),
        (1, 1, 1, 1800),
        (1200, 12000, 4194304, 14400),
        (1800, 18000, 8388608, 21600),
    ],
)
async def test_native_options_section_displays_saved_capture_preferences(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    operations: int,
    events: int,
    raw_bytes: int,
    duration: int,
) -> None:
    """Native reopening and unchanged Save preserve supported old/new preferences."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {
        "diagnostic_operations": operations,
        "diagnostic_events": events,
        "diagnostic_raw_bytes": raw_bytes,
        "diagnostic_duration": duration,
    }
    hass.config_entries.async_update_entry(entry, options=saved)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow",
        json={"handler": entry.entry_id},
    )
    assert response.status == 200
    result = await response.json()
    group = next(
        field for field in result["data_schema"] if field["name"] == "diagnostics"
    )
    assert group["expanded"] is False
    shown = {name.partition("__")[0]: value for name, value in group["default"].items()}
    assert {key: int(shown[key]) for key in saved} == saved
    assert shown["start_capture"] is False
    assert shown["restore_defaults"] is False
    assert dict(entry.options) == saved
    response = await client.post(
        f"/api/config/config_entries/options/flow/{result['flow_id']}", json={}
    )
    assert response.status == 200
    review = await response.json()
    assert {
        field["name"]: field["default"] for field in review["data_schema"]
    } == {"review_action": "no_changes"}
    assert dict(entry.options) == saved
    response = await client.post(
        f"/api/config/config_entries/options/flow/{review['flow_id']}", json={}
    )
    assert response.status == 200
    assert (await response.json())["type"] == FlowResultType.CREATE_ENTRY
    assert {key: entry.options[key] for key in saved} == saved
    assert entry.options[CONF_ENABLE_VERIFIED_CONTROLS] is False
    assert "start_capture" not in entry.options


async def test_native_capture_review_warns_for_unchanged_insufficient_capacity(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
) -> None:
    """Selected start warns without replacing saved values or starting on Next."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {
        "diagnostic_operations": 100,
        "diagnostic_events": 12000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 3600,
    }
    hass.config_entries.async_update_entry(entry, options=saved)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow",
        json={"handler": entry.entry_id},
    )
    assert response.status == 200
    opened = await response.json()
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json=capture_options_input(opened, {"diagnostics": {"start_capture": True}}),
    )
    assert response.status == 200
    review = await response.json()
    assert review["step_id"] == "confirm"
    fields = {field["name"]: field for field in review["data_schema"]}
    warning = fields["capture_capacity_warning"]
    assert warning["default"] == "shorter_than_duration"
    assert warning["selector"]["select"]["read_only"] is True
    assert (
        warning["selector"]["select"]["translation_key"]
        == "diagnostic_capture_capacity_warning"
    )
    assert fields["diagnostic_operations__unchanged"]["default"] == "100"
    assert fields["diagnostic_duration__unchanged"]["default"] == "3600"
    assert dict(entry.options) == saved
    hass.config_entries.options.async_abort(review["flow_id"])
    assert dict(entry.options) == saved


@pytest.mark.parametrize(
    ("operations", "polling_disabled", "values", "depth", "warns"),
    [
        (119, False, {"diagnostics": {"start_capture": True}}, 1785, True),
        (120, False, {"diagnostics": {"start_capture": True}}, 1800, False),
        (121, False, {"diagnostics": {"start_capture": True}}, 1815, False),
        (119, True, {"diagnostics": {"start_capture": True}}, 0, False),
        (119, False, {}, None, False),
        (119, False, {CONF_POLL_INTERVAL: 20}, None, False),
        (119, False, {"diagnostics": {"diagnostic_operations": 118}}, 1770, False),
        (
            119, False,
            {"diagnostics": {"diagnostic_duration": "21600", "start_capture": True}},
            1785, True,
        ),
    ],
)
async def test_native_capacity_warning_respects_start_polling_and_strict_boundary(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    operations: int,
    polling_disabled: bool,
    values: dict,
    depth: int | None,
    warns: bool,
) -> None:
    """Warning is informational, conditional and does not silently raise budgets."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {
        "diagnostic_operations": operations,
        "diagnostic_events": 12000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 1800,
    }
    hass.config_entries.async_update_entry(
        entry, options=saved, pref_disable_polling=polling_disabled
    )
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow",
        json={"handler": entry.entry_id},
    )
    assert response.status == 200
    opened = await response.json()
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json=capture_options_input(opened, values),
    )
    assert response.status == 200
    review = await response.json()
    assert review["step_id"] == "confirm"
    fields = {field["name"].partition("__")[0]: field for field in review["data_schema"]}
    assert ("capture_capacity_warning" in fields) is warns
    assert all(
        field["selector"]["select"]["read_only"]
        for field in fields.values()
    )
    if depth is not None:
        assert fields["estimated_seconds"]["default"] == str(depth)
    else:
        assert "estimated_seconds" not in fields
    if values.get("diagnostics", {}).get("start_capture"):
        assert fields["diagnostic_operations"]["default"] == str(operations)
        assert fields["diagnostic_events"]["default"] == "12000"
        assert fields["diagnostic_raw_bytes"]["default"] == "4194304"
        assert fields["capture_replacement"]["default"] == "replace"
    else:
        assert "capture_replacement" not in fields
    assert dict(entry.options) == saved
    hass.config_entries.options.async_abort(review["flow_id"])
    assert dict(entry.options) == saved


async def test_explicit_capture_start_failure_keeps_confirmation_open(
    hass: HomeAssistant,
) -> None:
    """A disabled/unloaded entry cannot report successful capture consent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MODEL,
        data={CONF_HOST: HOST, CONF_PORT: PORT, CONF_DEVICE_ID: DEVICE_ID},
        unique_id=DEVICE_ID,
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], capture_options_input(result, {"diagnostics": {"start_capture": True}})
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "capture_start_failed"}
    assert result["data_schema"]({})["review_action"] == "start"
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert not entry.options
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})["diagnostics"]["start_capture"] is False
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "start_capture" not in entry.options


async def test_capture_start_and_preference_only_save_preserve_consent_boundary(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Checked Save starts once; later unchecked preferences do not replace it."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch.object(
            UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
        ) as read_state,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_entry,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            capture_options_input(result, {"diagnostics": {
                "diagnostic_operations": 2,
                "diagnostic_events": 20,
                "diagnostic_raw_bytes": 532,
                "diagnostic_duration": "1800",
                "start_capture": True,
            }}),
        )
        assert not coordinator.trace.active
        assert (
            result["data_schema"]({})["capture_capacity_warning"]
            == "shorter_than_duration"
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert coordinator.trace.active
        started = coordinator.capture_status
        assert started["limits"] == {"operations": 2, "events": 20, "raw_bytes": 532}
        assert started["started_at"] is not None
        assert started["end_at"] is not None
        assert 0 < started["remaining_seconds"] <= 1800
        assert "start_capture" not in entry.options
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            capture_options_input(result, {"diagnostics": {"diagnostic_operations": 3}}),
        )
        displayed = result["data_schema"]({})
        assert displayed["diagnostic_operations__changed"] == "2 → 3"
        assert displayed["review_action"] == "preferences"
        assert displayed["capture_preferences"] == "next_capture"
        assert "capture_replacement" not in displayed
        assert "capture_capacity_warning" not in displayed
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.runtime_data is coordinator
        assert coordinator.trace.active
        after = coordinator.capture_status
        for key in ("limits", "retained", "started_at", "end_at"):
            assert after[key] == started[key]
        assert entry.options["diagnostic_operations"] == 3
        assert "start_capture" not in entry.options
        reload_entry.assert_not_awaited()
        read_state.assert_awaited_once()
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, {"review_action": "no_changes"}),
        (
            {CONF_POLL_INTERVAL: 20},
            {"review_action": "preferences", "poll_interval": "15 → 20"},
        ),
        (
            {"diagnostics": {"diagnostic_operations": 2}},
            {
                "review_action": "preferences",
                "diagnostic_operations": "1800 → 2",
                "estimated_seconds": "30",
                "capture_preferences": "next_capture",
            },
        ),
        (
            {CONF_ENABLE_VERIFIED_CONTROLS: True},
            {"review_action": "preferences", "controls_change": "enable"},
        ),
        (
            {
                CONF_ENABLE_VERIFIED_CONTROLS: True,
                "diagnostics": {"diagnostic_operations": 2},
            },
            {
                "review_action": "preferences",
                "controls_change": "enable",
                "diagnostic_operations": "1800 → 2",
                "estimated_seconds": "30",
            },
        ),
        (
            {"diagnostics": {"start_capture": True}},
            {
                "review_action": "start",
                "diagnostic_operations": "1800",
                "diagnostic_events": "18000",
                "diagnostic_raw_bytes": "4194304",
                "diagnostic_duration": "21600",
                "estimated_seconds": "27000",
                "capture_replacement": "replace",
            },
        ),
    ],
)
async def test_review_contains_only_effective_changes_and_selected_actions(
    hass: HomeAssistant, values: dict, expected: dict,
) -> None:
    """Default normalization is not a change; starting is an explicit separate action."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], capture_options_input(result, values)
    )
    assert {
        name.partition("__")[0]: value for name, value in result["data_schema"]({}).items()
    } == expected
    assert not entry.options
    hass.config_entries.options.async_abort(result["flow_id"])
    assert not entry.options


async def test_review_cannot_inject_start_consent(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
) -> None:
    """The final informational form cannot acquire a start choice absent on Next."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {"start_capture": True}
            )
        assert not entry.runtime_data.trace.active
        assert not entry.options
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert not entry.runtime_data.trace.active
        assert "start_capture" not in entry.options
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diagnostic_operations", 0),
        ("diagnostic_operations", "not a number"),
        ("diagnostic_events", 18001),
        ("diagnostic_events", True),
        ("diagnostic_raw_bytes", 1.5),
        ("diagnostic_raw_bytes", None),
        ("diagnostic_raw_bytes", ""),
        ("diagnostic_duration", "3601"),
        ("diagnostic_duration", 7200),
    ],
)
async def test_native_restore_recovers_invalid_diagnostic_input_only_on_confirmation(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, key: str, value: object,
) -> None:
    """Native pre-handler validation must allow an explicit recovery to defaults."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {
        CONF_POLL_INTERVAL: 20,
        "diagnostic_operations": 600,
        "diagnostic_events": 12000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 7200,
    }
    hass.config_entries.async_update_entry(entry, options=saved)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    opened = await response.json()
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json=capture_options_input(opened, {"diagnostics": {
            key: value, "restore_defaults": True,
        }}),
    )
    assert response.status == 200
    review = await response.json()
    assert review["step_id"] == "confirm"
    assert dict(entry.options) == saved
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}", json={}
    )
    assert response.status == 200
    assert (await response.json())["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {
        CONF_ENABLE_VERIFIED_CONTROLS: False,
        CONF_POLL_INTERVAL: 20,
        "diagnostic_operations": 1800,
        "diagnostic_events": 18000,
        "diagnostic_raw_bytes": 4194304,
        "diagnostic_duration": 21600,
    }


async def test_native_capture_descriptions_keep_opening_baseline_after_invalid_edit(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    """Native field variants distinguish saved, matching and default provenance."""
    entry = _entry()
    entry.add_to_hass(hass)
    saved = {"diagnostic_operations": 600, "diagnostic_raw_bytes": 4194304}
    hass.config_entries.async_update_entry(entry, options=saved)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    opened = await response.json()
    group = next(field for field in opened["data_schema"] if field["name"] == "diagnostics")
    fields = {field["name"]: field for field in group["schema"]}
    assert "diagnostic_operations__saved" in fields
    assert "diagnostic_raw_bytes__matching" in fields
    assert "diagnostic_events__default" in fields
    assert "diagnostic_duration__default" in fields
    baseline = opened["description_placeholders"]
    assert baseline["diagnostic_operations_saved"] == "600"
    assert baseline["diagnostic_operations_default"] == "1800"
    hass.config_entries.async_update_entry(
        entry, options={**saved, "diagnostic_operations": 700}
    )
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json=capture_options_input(opened, {"diagnostics": {
            "diagnostic_operations": "not a number", "start_capture": True,
        }}),
    )
    assert response.status == 200
    invalid = await response.json()
    assert invalid["step_id"] == "init"
    assert invalid["errors"] == {"base": "invalid_options"}
    assert invalid["description_placeholders"] == baseline
    group = next(field for field in invalid["data_schema"] if field["name"] == "diagnostics")
    fields = {field["name"]: field for field in group["schema"]}
    assert fields["diagnostic_operations__saved"]["description"]["suggested_value"] == "not a number"
    assert fields["start_capture"]["description"]["suggested_value"] is True
    initial = group.get("description", {}).get("suggested_value", group.get("default"))
    assert initial["diagnostic_operations__saved"] == "not a number"
    assert initial["start_capture"] is True
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json={"diagnostics": {
            "diagnostic_operations__saved": "not a number", "restore_defaults": True,
        }},
    )
    assert response.status == 200
    review = await response.json()
    fields = {field["name"]: field for field in review["data_schema"]}
    assert fields["diagnostic_operations__changed"]["default"] == "600 → 1800"
    assert entry.options["diagnostic_operations"] == 700
    hass.config_entries.options.async_abort(review["flow_id"])


async def test_native_restore_keeps_unrelated_validation_and_can_be_cancelled(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    """Restore never overrides polling validation or grants early save/start."""
    entry = _entry()
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    opened = await response.json()
    values = capture_options_input(opened, {
        CONF_POLL_INTERVAL: 0,
        "diagnostics": {
            "diagnostic_operations": "invalid",
            "restore_defaults": True, "start_capture": True,
        },
    })
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}", json=values
    )
    assert response.status == 400
    assert not entry.options
    values[CONF_POLL_INTERVAL] = 15
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}", json=values
    )
    assert response.status == 200
    review = await response.json()
    fields = {field["name"]: field for field in review["data_schema"]}
    assert fields["restore_action"]["default"] == "already_default"
    assert fields["review_action"]["default"] == "start"
    assert fields["diagnostic_operations__unchanged"]["default"] == "1800"
    assert "capture_replacement" in fields
    assert not entry.options
    response = await client.delete(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}"
    )
    assert response.status == 200
    assert not entry.options
    response = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    reopened = await response.json()
    group = next(field for field in reopened["data_schema"] if field["name"] == "diagnostics")
    assert group["default"]["restore_defaults"] is False
    assert group["default"]["start_capture"] is False
    await client.delete(f"/api/config/config_entries/options/flow/{reopened['flow_id']}")


@pytest.mark.parametrize("start", [False, True])
async def test_restore_preserves_active_capture_unless_separate_start_selected(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot, start: bool,
) -> None:
    """Restored next-capture settings cannot resize/erase an existing capture."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)) as read_state:
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            coordinator = entry.runtime_data
            opened = await hass.config_entries.options.async_init(entry.entry_id)
            review = await hass.config_entries.options.async_configure(
                opened["flow_id"], capture_options_input(opened, {"diagnostics": {
                    "diagnostic_operations": 2, "diagnostic_events": 20,
                    "diagnostic_raw_bytes": 532, "diagnostic_duration": "1800",
                    "start_capture": True,
                }}),
            )
            await hass.config_entries.options.async_configure(review["flow_id"], {})
            await coordinator.async_request_refresh()
            before = coordinator.trace.export()
            deadline = coordinator.capture_status["end_at"]
            reads = read_state.await_count
            opened = await hass.config_entries.options.async_init(entry.entry_id)
            review = await hass.config_entries.options.async_configure(
                opened["flow_id"], capture_options_input(opened, {"diagnostics": {
                    "diagnostic_operations": "", "restore_defaults": True, "start_capture": start,
                }}),
            )
            assert coordinator.trace.export() == before
            assert coordinator.capture_status["end_at"] == deadline
            displayed = review["data_schema"]({})
            assert displayed["restore_action"] == "restore"
            assert ("capture_replacement" in displayed) is start
            assert "capture_capacity_warning" not in displayed
            await hass.config_entries.options.async_configure(review["flow_id"], {})
            await hass.async_block_till_done()
            assert entry.runtime_data is coordinator
            assert read_state.await_count == reads
            assert entry.options["diagnostic_operations"] == 1800
            assert entry.options["diagnostic_duration"] == 21600
            assert "restore_defaults" not in entry.options
            assert "start_capture" not in entry.options
            assert coordinator.trace.active
            if start:
                assert coordinator.capture_status["limits"]["operations"] == 1800
                assert coordinator.capture_status["end_at"] > deadline
                assert coordinator.trace.export()["operations"] == []
            else:
                assert coordinator.trace.export() == before
                assert coordinator.capture_status["end_at"] == deadline
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_cleared_budget_requires_restore(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    """A browser-cleared optional string is omitted, not a request for its old default."""
    entry = _entry()
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    opened = await (await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )).json()
    group = next(field for field in opened["data_schema"] if field["name"] == "diagnostics")
    values = dict(group["default"])
    del values["diagnostic_operations__default"]
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json={"diagnostics": values},
    )
    assert response.status == 200
    invalid = await response.json()
    assert invalid["step_id"] == "init"
    assert invalid["errors"] == {"base": "invalid_options"}
    assert not entry.options
    response = await client.post(
        f"/api/config/config_entries/options/flow/{opened['flow_id']}",
        json={"diagnostics": {**values, "restore_defaults": True}},
    )
    assert response.status == 200
    review = await response.json()
    assert review["step_id"] == "confirm"
    assert not entry.options
    hass.config_entries.options.async_abort(review["flow_id"])
