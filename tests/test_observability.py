"""Behavioral contracts for private logs and cached troubleshooting state."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.uplus_reborn.coordinator import UplusRebornCoordinator
from custom_components.uplus_reborn.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    DeviceSnapshot,
    OutcomeUnknown,
    UnsafeState,
    UwtClient,
    classify_error,
)

from .conftest import DEVICE_ID, HOST
from .test_integration import _entry
from .test_protocol import (
    _active_spin_words,
    _frame_response,
    _read_request,
    _snapshot,
    _standby_words,
    _state_response,
)

LOGGER = "custom_components.uplus_reborn"
OPAQUE_ID = "PRIVATE-OPAQUE-UWT-IDENTIFIER"
ACCOUNT = "private-account@example.invalid"
TOKEN = "private-access-token-value"


def _private_response(packet: bytes) -> bytes:
    """Put synthetic complete identifiers in an otherwise valid envelope."""
    return (
        packet[:8]
        + OPAQUE_ID.encode().ljust(32, b"\0")
        + DEVICE_ID.encode().ljust(32, b"\0")
        + packet[72:]
    )


def _assert_private(text: str) -> None:
    for secret in (HOST, DEVICE_ID, OPAQUE_ID, ACCOUNT, TOKEN, "127.0.0.1"):
        assert secret not in text


def _coordinator(hass: HomeAssistant, *, controls: bool = False):
    entry = _entry(controls=controls)
    entry.add_to_hass(hass)
    coordinator = UplusRebornCoordinator(hass, entry, UwtClient(HOST, DEVICE_ID))
    entry.runtime_data = coordinator
    return entry, coordinator


async def test_snapshot_logs_only_meaningful_deltas_and_health_ages(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated observations change freshness, not drum delta output."""
    _, coordinator = _coordinator(hass)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    changed = replace(
        standby_snapshot,
        upper=replace(standby_snapshot.upper, cycle_count=42),
        current_water_raw=3,
    )
    now = datetime(2026, 10, 17, tzinfo=UTC)
    with (
        patch.object(
            UwtClient,
            "read_state",
            AsyncMock(
                side_effect=[
                    standby_snapshot,
                    replace(standby_snapshot, observed_at=now),
                    changed,
                ]
            ),
        ),
        patch("custom_components.uplus_reborn.coordinator.datetime") as clock,
    ):
        clock.now.return_value = now
        await coordinator.async_refresh()
        initial = [
            record
            for record in caplog.records
            if "event=initial_snapshot" in record.message
        ]
        assert len(initial) == 2
        original_health = coordinator.health
        assert original_health.freshness == "fresh"
        assert original_health.age_seconds == 0
        with pytest.raises(FrozenInstanceError):
            original_health.available = False
        caplog.clear()
        await coordinator.async_refresh()
        assert not any(
            "event=delta" in record.message
            or "event=initial_snapshot" in record.message
            or "event=shared_delta" in record.message
            for record in caplog.records
        )
        await coordinator.async_refresh()
        deltas = [
            record for record in caplog.records if "event=delta" in record.message
        ]
        assert len(deltas) == 1
        assert deltas[0].args[-1] == {"cycle_count": 42}
        assert "drum=upper" in deltas[0].message
        shared = [
            record
            for record in caplog.records
            if "event=shared_delta" in record.message
        ]
        assert shared[-1].args[-1] == {"current_water_raw": 3}
        clock.now.return_value = now + timedelta(seconds=31)
        assert coordinator.health.freshness == "stale"
        assert coordinator.health.age_seconds == 31
        assert original_health.age_seconds == 0
    _assert_private(caplog.text)


async def test_debug_disabled_does_not_build_snapshot_dictionaries(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Normal polling keeps the original immutable object without delta work."""
    _, coordinator = _coordinator(hass)
    caplog.set_level(logging.INFO, logger=LOGGER)
    with (
        patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)),
        patch(
            "custom_components.uplus_reborn.coordinator.fields",
            side_effect=AssertionError("delta work"),
        ),
    ):
        await coordinator.async_refresh()
    assert coordinator.data is standby_snapshot
    assert coordinator.health.available


async def test_no_data_and_exception_privacy_are_available_without_setup(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Before first success, cache the safe cause rather than exception text."""
    entry, coordinator = _coordinator(hass)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert coordinator.health.freshness == "never_read"
    error = CannotConnect(
        f"{HOST} {DEVICE_ID} {OPAQUE_ID} {ACCOUNT} {TOKEN}",
        context=((TOKEN, 1), ("inner_offset", 7)),
    )
    error.__cause__ = OSError(f"failed to connect to {HOST}")
    with patch.object(UwtClient, "read_state", AsyncMock(side_effect=error)):
        await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.health.last_success is None
    assert coordinator.health.last_error.cause == "connection_error"
    assert not coordinator.health.available
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["device"]["state"] is None
    assert result["health"]["last_error"]["cause"] == "connection_error"
    assert result["health"]["last_error"]["context"] == {"inner_offset": 7}
    _assert_private(json.dumps(result) + caplog.text + str(coordinator.last_exception))
    assert classify_error(UnsafeState(TOKEN, reason=TOKEN)) == "unsafe_state"


async def test_unloaded_diagnostics_allowlist_configuration(
    hass: HomeAssistant,
) -> None:
    """An entry without runtime data needs neither a network call nor credentials."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "account": ACCOUNT, "token": TOKEN, "opaque": OPAQUE_ID},
        options={"token": TOKEN},
    )
    with patch.object(UwtClient, "read_state", side_effect=AssertionError("probe")):
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["health"]["freshness"] == "unloaded"
    assert not result["health"]["available"]
    assert result["device"]["state"] is None
    assert result["staged_intents"] == {"upper": None, "lower": None}
    _assert_private(json.dumps(result))


async def test_poll_failure_recovery_preserves_cached_state_without_duplicate_noise(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DUC owns one failure/recovery notice; diagnostics distinguish stale data."""
    entry = _entry()
    entry.add_to_hass(hass)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    error = CannotConnect(f"{HOST} {DEVICE_ID} {TOKEN}")
    with patch.object(
        UwtClient,
        "read_state",
        AsyncMock(
            side_effect=[
                standby_snapshot,
                error,
                error,
                standby_snapshot,
            ]
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        failed = await async_get_config_entry_diagnostics(hass, entry)
        assert failed["health"]["freshness"] == "unavailable"
        assert failed["health"]["last_success"] is not None
        assert failed["device"]["state"]["upper"]["phase_value"] == 0
        failures = [
            record
            for record in caplog.records
            if record.name == f"{LOGGER}.coordinator"
            and record.levelno >= logging.WARNING
        ]
        assert len(failures) == 1
        await coordinator.async_refresh()
        recovered = await async_get_config_entry_diagnostics(hass, entry)
        assert recovered["health"]["freshness"] == "fresh"
        assert recovered["health"]["last_error"] is not None
        recovery = [
            record
            for record in caplog.records
            if record.name == f"{LOGGER}.coordinator" and record.levelno == logging.INFO
        ]
        assert len(recovery) == 1
        _assert_private(json.dumps(recovered) + caplog.text)
        assert await hass.config_entries.async_unload(entry.entry_id)
        unloaded = await async_get_config_entry_diagnostics(hass, entry)
        assert unloaded["health"]["freshness"] == "unloaded"
        assert not unloaded["health"]["available"]


@pytest.mark.usefixtures("loopback_socket")
@pytest.mark.parametrize(
    "response", ["confirmed", "wrong_sequence", "unexpected_type", "independent_query"]
)
async def test_control_logs_proof_boundaries_without_identifiers_or_retry(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    response: str,
) -> None:
    """Unclassified replies need independent query proof, never a write retry."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    requests = []
    closed = asyncio.Event()
    confirmed = response in ("confirmed", "independent_query")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            requests.append(await _read_request(reader))
            writer.write(
                _private_response(_state_response(_standby_words(), frame_type=0x02))
            )
            await writer.drain()
            requests.append(await _read_request(reader))
            writer.write(_private_response(_frame_response(0x74, b"", sequence=1)))
            await writer.drain()
            requests.append(await _read_request(reader))
            frame_type = (
                0x05 if response in ("unexpected_type", "independent_query") else 0x03
            )
            sequence = 9 if response == "wrong_sequence" else 2
            writer.write(
                _private_response(
                    _frame_response(
                        frame_type,
                        b"" if frame_type == 0x05 else b"\x00\x00",
                        sequence=sequence,
                    )
                )
                + _private_response(_state_response(_active_spin_words()))
            )
            await writer.drain()
            if response == "independent_query":
                request = await _read_request(reader)
                requests.append(request)
                assert request[1][9:12] == b"\x01\x4d\x01"
                writer.write(
                    _private_response(
                        _state_response(
                            _active_spin_words(), sequence=request[0], frame_type=0x02
                        )
                    )
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        entry, coordinator = _coordinator(hass, controls=True)
        coordinator.client = UwtClient(
            "127.0.0.1", DEVICE_ID, port=server.sockets[0].getsockname()[1]
        )
        coordinator.async_set_updated_data(_snapshot(_standby_words()))
        coordinator.stage_program("upper", "spin")
        coordinator.stage_settings("upper", spin_rpm=400)
        if confirmed:
            result = await coordinator.async_start_program("upper")
            assert result.after.upper.phase_value == 0x0B
        else:
            with pytest.raises(OutcomeUnknown):
                await coordinator.async_start_program("upper")
        await asyncio.wait_for(closed.wait(), 1)
        health = coordinator.health
        assert health.latest_command.outcome == (
            "confirmed" if confirmed else "unknown"
        )
        assert not health.latest_command.retried
        assert coordinator.draft("upper") is None
        assert len(requests) == (4 if response == "independent_query" else 3)
        assert sum(packet[9] == 0x60 for _, packet in requests) == 1
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        if confirmed:
            assert (
                "spin_rpm"
                in diagnostics["health"]["latest_command"]["unconfirmed_settings"]
            )
    messages = [record.message for record in caplog.records]
    assert (
        any("event=postcondition_observed" in message for message in messages)
        is confirmed
    )
    control_correlation = [
        message
        for message in messages
        if "event=correlated" in message and "sequence=2 " in message
    ]
    assert bool(control_correlation) == (response == "confirmed")
    assert all(
        "session=" in message and "elapsed=" in message
        for message in messages
        if "event=response " in message
    )
    _assert_private(caplog.text + json.dumps(diagnostics))
    for _, packet in requests:
        assert packet.hex() not in caplog.text


async def test_precondition_rejection_does_not_consume_or_dispatch(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Permission is checked under the lock, independently of staged intent."""
    entry, coordinator = _coordinator(hass)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    coordinator.async_set_updated_data(standby_snapshot)
    coordinator.stage_program("upper", "spin")
    with (
        patch.object(
            UwtClient, "start_program", side_effect=AssertionError("dispatch")
        ),
        pytest.raises(UnsafeState),
    ):
        await coordinator.async_start_program("upper")
    assert coordinator.draft("upper").program == "spin"
    assert coordinator.health.latest_command.outcome == "not_dispatched"
    assert coordinator.health.latest_command.cause == "controls_disabled"
    result = await async_get_config_entry_diagnostics(hass, entry)
    _assert_private(json.dumps(result) + caplog.text)


async def test_offline_first_setup_retains_classified_diagnostics(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A loaded offline entry has no fabricated state and retains safe diagnostics."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(
        UwtClient, "read_state", AsyncMock(side_effect=CannotConnect(TOKEN))
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["device"]["state"] is None
    assert result["health"]["entry_state"] == ConfigEntryState.LOADED.value
    assert result["health"]["freshness"] == "never_read"
    assert result["health"]["last_error"]["cause"] == "connection_closed"
    assert result["health"]["loaded"]
    assert not result["health"]["available"]
    _assert_private(json.dumps(result) + caplog.text)


async def test_enabling_debug_after_setup_logs_a_baseline(
    hass: HomeAssistant,
    standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA's debug action gets a baseline even after prior non-debug polling."""
    _, coordinator = _coordinator(hass)
    caplog.set_level(logging.INFO, logger=LOGGER)
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ):
        await coordinator.async_refresh()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        await coordinator.async_refresh()
    assert (
        len(
            [
                record
                for record in caplog.records
                if "event=initial_snapshot" in record.message
            ]
        )
        == 2
    )


@pytest.mark.usefixtures("loopback_socket")
@pytest.mark.parametrize(
    "cause", ["partial_envelope", "invalid_escape", "checksum_mismatch"]
)
async def test_malformed_stream_logs_only_bounded_context(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    cause: str,
) -> None:
    """Framing failures expose classification, never bytes or identifiers."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    closed = asyncio.Event()
    packet = _private_response(_state_response(_standby_words()))
    if cause == "partial_envelope":
        malformed = packet[:55]
    elif cause == "invalid_escape":
        # Inner offset 10 is the command's high byte; the next byte is not 55.
        malformed = packet[:90] + b"\xff" + packet[91:]
    else:
        malformed = packet[:-1] + bytes((packet[-1] ^ 1,))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await _read_request(reader)
        writer.write(malformed)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        entry, coordinator = _coordinator(hass)
        coordinator.client = UwtClient(
            "127.0.0.1", DEVICE_ID, port=server.sockets[0].getsockname()[1]
        )
        await coordinator.async_refresh()
        await asyncio.wait_for(closed.wait(), 1)
        assert coordinator.health.last_error.cause == cause
        assert coordinator.health.last_success is None
        result = await async_get_config_entry_diagnostics(hass, entry)
        context = result["health"]["last_error"]["context"]
        if cause == "partial_envelope":
            assert context == {"envelope_received_length": 55}
        elif cause == "invalid_escape":
            assert context["inner_offset"] == 10
            assert context["inner_received_length"] == len(malformed) - 80
        else:
            assert context["inner_offset"] == len(malformed) - 81
            assert context["inner_declared_length"] == malformed[82]
            assert context["inner_received_length"] == len(malformed) - 80
    buffered = 55 if cause == "partial_envelope" else 0
    assert any(
        f"incomplete_bytes={buffered}" in record.message for record in caplog.records
    )
    assert malformed.hex() not in caplog.text
    assert repr(malformed) not in caplog.text
    _assert_private(caplog.text + json.dumps(result))


def test_loopback_fixture_rejects_external_connections(loopback_socket: None) -> None:
    """Socket construction does not authorize any non-loopback destination."""
    with (
        socket.socket() as connection,
        pytest.raises(pytest_socket.SocketConnectBlockedError),
    ):
        connection.connect(("192.0.2.1", 9))


@pytest.mark.usefixtures("loopback_socket")
@pytest.mark.parametrize("correlated", [True, False])
async def test_cleanup_cancellation_preserves_dispatched_outcome(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    correlated: bool,
) -> None:
    """Cancellation cannot relabel an already-sent command as not dispatched."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    requests = []
    closing = asyncio.Event()
    server_closed = asyncio.Event()
    original_connect = asyncio.open_connection

    async def connect(*args, **kwargs):
        reader, writer = await original_connect(*args, **kwargs)
        original_wait_closed = writer.wait_closed

        async def hold_cleanup():
            closing.set()
            try:
                await asyncio.Event().wait()
            finally:
                await original_wait_closed()

        monkeypatch.setattr(writer, "wait_closed", hold_cleanup)
        return reader, writer

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            requests.append(await _read_request(reader))
            writer.write(_state_response(_standby_words(), frame_type=0x02))
            await writer.drain()
            requests.append(await _read_request(reader))
            writer.write(_frame_response(0x74, b"", sequence=1))
            await writer.drain()
            requests.append(await _read_request(reader))
            if correlated:
                writer.write(_frame_response(0x03, b"\x00\x00", sequence=2))
            writer.write(_state_response(_active_spin_words()))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            server_closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        entry, coordinator = _coordinator(hass, controls=True)
        coordinator.client = UwtClient(
            "127.0.0.1", DEVICE_ID, port=server.sockets[0].getsockname()[1]
        )
        coordinator.async_set_updated_data(_snapshot(_standby_words()))
        coordinator.stage_program("upper", "spin")
        coordinator.stage_settings("upper", spin_rpm=400)
        monkeypatch.setattr(asyncio, "open_connection", connect)
        command = asyncio.create_task(coordinator.async_start_program("upper"))
        await asyncio.wait_for(closing.wait(), 1)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        await asyncio.wait_for(server_closed.wait(), 1)
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    outcome = diagnostics["health"]["latest_command"]
    assert outcome["outcome"] == "unknown"
    assert outcome["cause"] == "cancelled"
    assert not outcome["retried"]
    assert len(requests) == 3
    _assert_private(caplog.text + json.dumps(diagnostics))


async def test_loaded_draft_diagnostics_are_private_cached_and_clear_on_unload(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot
) -> None:
    """Both local drafts can be diagnosed without serializing entry secrets or probing."""
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "account": ACCOUNT, "token": TOKEN, "opaque": OPAQUE_ID},
    )
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ) as read_state:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_fabric("upper", "B1")
        coordinator.stage_settings("upper", stains=("W30", "W1"))
        coordinator.stage_program("lower", "dry")
        coordinator.stage_settings("lower", drying_mode="30_min")
        with (
            patch("asyncio.open_connection", side_effect=AssertionError("probe")),
            patch.object(
                UwtClient, "start_program", side_effect=AssertionError("write")
            ),
            patch.object(
                UwtClient, "control_drum", side_effect=AssertionError("write")
            ),
            patch.object(
                UwtClient, "set_appliance_option", side_effect=AssertionError("write")
            ),
        ):
            result = await async_get_config_entry_diagnostics(hass, entry)
            assert result["staged_intents"]["upper"]["fabric"] == "B1"
            assert result["staged_intents"]["upper"]["stains"] == ("W1", "W30")
            assert result["staged_intents"]["lower"]["drying_mode"] == "30_min"
            assert result["device"]["state"]["lower"]["program"] == "none"
            _assert_private(json.dumps(result))
            assert await hass.config_entries.async_unload(entry.entry_id)
            unloaded = await async_get_config_entry_diagnostics(hass, entry)
            assert unloaded["staged_intents"] == {"upper": None, "lower": None}
            assert not unloaded["health"]["available"]
            _assert_private(json.dumps(unloaded))
        read_state.assert_awaited_once_with()
