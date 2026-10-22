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
from pytest_homeassistant_custom_component.common import MockConfigEntry

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

from .conftest import DEVICE_ID, HOST, PORT

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
    """The options flow is the only route that exposes live controls."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({}) == {
        "connection_status": "unknown",
        CONF_ENABLE_VERIFIED_CONTROLS: False,
        CONF_POLL_INTERVAL: 15,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENABLE_VERIFIED_CONTROLS: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_ENABLE_VERIFIED_CONTROLS: True,
        CONF_POLL_INTERVAL: 15,
    }


@pytest.mark.parametrize(
    ("reading", "failed", "status"),
    [
        ("fresh", False, "connected"),
        ("fresh", True, "offline"),
        ("stale", False, "unknown"),
        ("never", False, "unknown"),
        ("never", True, "offline"),
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
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {
        CONF_ENABLE_VERIFIED_CONTROLS: False,
        CONF_POLL_INTERVAL: interval,
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
