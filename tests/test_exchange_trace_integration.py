"""Native DEBUG lifecycle and downloaded trace consumer contracts."""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.uplus_reborn.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.uplus_reborn.protocol import (
    DeviceSnapshot,
    OutcomeUnknown,
    UnsafeState,
    UwtClient,
)

from .test_integration import _entry
from .test_protocol import (
    _accept_control_initialization,
    _active_spin_words,
    _frame_response,
    _read_request,
    _standby_words,
    _state_response,
)

LOGGER = "custom_components.uplus_reborn"


async def _level(hass: HomeAssistant, level: str, logger: str = LOGGER) -> None:
    await hass.services.async_call("logger", "set_level", {logger: level}, blocking=True)
    await hass.async_block_till_done()


async def test_native_debug_freezes_download_and_resets_without_appliance_events(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    initial = await async_get_config_entry_diagnostics(hass, entry)
    assert initial["exchange_trace"]["operations"] == []
    await _level(hass, "debug")
    with (
        patch.object(UwtClient, "start_program", side_effect=AssertionError("dispatch")),
        patch("asyncio.open_connection", side_effect=AssertionError("network")),
    ):
        with pytest.raises(UnsafeState):
            await coordinator.async_start_program("upper")
        recording = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
        operation = recording["operations"][-1]
        assert operation["kind"] == "upper_start"
        assert operation["result"] == {"outcome": "not_dispatched", "cause": "program_not_staged"}
        await _level(hass, "warning", "unrelated.integration")
        assert (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"] == recording
        await _level(hass, "info")
        stopped = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
        assert stopped["capture_state"] == "stopped"
        assert stopped["operations"] == recording["operations"]
        assert (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"] == stopped
        await _level(hass, "debug")
        fresh = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
        assert fresh["operations"] == []
        await _level(hass, "info")
    assert await hass.config_entries.async_unload(entry.entry_id)
    unloaded = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
    assert unloaded["capture_state"] == "closed"
    assert unloaded["operations"] == []
    assert coordinator.trace.export()["capture_state"] == "closed"
    assert coordinator.trace.export()["operations"] == []


@pytest.mark.usefixtures("loopback_socket")
@pytest.mark.parametrize("enabled", [False, True])
async def test_runtime_trace_observes_real_control_without_adding_traffic(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, enabled: bool,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "debug" if enabled else "info")
    connections = []
    closed = []
    peer_tasks = []
    errors = []
    before = _state_response(_standby_words(), frame_type=0x02)
    after = _frame_response(0x03, b"\x00\x00", sequence=2) + _state_response(_active_spin_words())
    init = _frame_response(0x74, bytes.fromhex("0f5a0000000000000000000000000000"), sequence=1)

    async def serve(reader, writer):
        requests = []
        connections.append(requests)
        try:
            requests.append(await _read_request(reader))
            writer.write(before[:3])
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(before[3:])
            await writer.drain()
            if len(connections) == 2:
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                writer.write(after)
                await writer.drain()
            assert await reader.read() == b""
        except (AssertionError, OSError, ValueError, asyncio.IncompleteReadError) as error:
            errors.append(error)
        finally:
            writer.close()
            await writer.wait_closed()
            closed.append(True)

    def connected(reader, writer):
        peer_tasks.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    async with server:
        entry = _entry(controls=True)
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, data={
            **entry.data, CONF_HOST: "127.0.0.1",
            CONF_PORT: server.sockets[0].getsockname()[1],
        })
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.stage_program("upper", "spin")
        coordinator.stage_settings("upper", spin_rpm=400)
        result = await coordinator.async_start_program("upper")
        assert result.after.upper.phase_value == 0x0B
        await asyncio.wait_for(asyncio.gather(*peer_tasks), 2)
        assert not errors
        assert closed == [True, True]
        assert [len(requests) for requests in connections] == [1, 3]
        assert [sequence for sequence, _ in connections[1]] == [0, 1, 2]
        trace = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
        json.dumps(trace)
        if enabled:
            assert [operation["kind"] for operation in trace["operations"]] == ["poll", "upper_start"]
            operation = trace["operations"][-1]
            assert operation["result"] == {"outcome": "confirmed", "cause": None}
            received = b"".join(
                bytes.fromhex(event["data_hex"])
                for event in operation["events"]
                if event["event"] == "raw" and event["direction"] == "rx"
            )
            assert received == before + init + after
            assert operation["context"]["host"] == "127.0.0.1"
            assert not operation["partial"]
        else:
            assert trace["operations"] == []
        await _level(hass, "info")
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_debug_is_domain_wide_but_entry_histories_and_unload_are_isolated(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    first, second = _entry(controls=True), _entry(controls=True, device_id="AABBCCDDEE02")
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        for entry in (first, second):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _level(hass, "debug")
        old = first.runtime_data
        with pytest.raises(UnsafeState):
            await old.async_start_program("upper")
        assert (await async_get_config_entry_diagnostics(hass, second))["exchange_trace"]["operations"] == []
        with pytest.raises(UnsafeState):
            await second.runtime_data.async_start_program("lower")
        assert [op["kind"] for op in (await async_get_config_entry_diagnostics(hass, first))["exchange_trace"]["operations"]] == ["upper_start"]
        second_trace = (await async_get_config_entry_diagnostics(hass, second))["exchange_trace"]
        assert [op["kind"] for op in second_trace["operations"]] == ["lower_start"]
        assert await hass.config_entries.async_unload(first.entry_id)
        assert old.trace.export()["capture_state"] == "closed"
        old.trace.event("late_completion")
        old.trace.finish("confirmed")
        assert old.trace.export()["operations"] == []
        assert (await async_get_config_entry_diagnostics(hass, second))["exchange_trace"] == second_trace
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        assert first.runtime_data is not old
        reloaded = (await async_get_config_entry_diagnostics(hass, first))["exchange_trace"]
        assert all(op["kind"] != "upper_start" for op in reloaded["operations"])
        await _level(hass, "info")
        for entry in (first, second):
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_capture_start_and_stop_mid_poll_are_partial_not_poll_failure(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_read():
        entered.set()
        await release.wait()
        return standby_snapshot

    with patch.object(UwtClient, "read_state", AsyncMock(side_effect=pending_read)):
        refresh = asyncio.create_task(coordinator.async_refresh())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await _level(hass, "debug")
            active = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert active["operations"][-1]["partial"]
            await _level(hass, "info")
            stopped = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert stopped["operations"][-1]["partial"]
        finally:
            release.set()
            await asyncio.wait_for(refresh, 2)
    assert coordinator.health.available
    assert (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"] == stopped
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_draft_intent_survives_capture_start_and_restart_during_start(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator.stage_program("upper", "spin")
    coordinator.stage_settings("upper", spin_rpm=400)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_control(intent, **kwargs):
        entered.set()
        await release.wait()
        raise OutcomeUnknown("Synthetic unproven command")

    with patch.object(UwtClient, "start_program", AsyncMock(side_effect=pending_control)):
        command = asyncio.create_task(coordinator.async_start_program("upper"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert coordinator.draft("upper") is None
            for _ in range(2):
                await _level(hass, "debug")
                trace = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
                operation = trace["operations"][-1]
                assert operation["partial"]
                requested = operation["context"]["requested_intent"]
                assert requested["drum"] == "upper"
                assert requested["program"] == "spin"
                assert requested["spin_rpm"] == 400
                assert operation["context"]["cached_snapshot"]["upper"]["program"] == "none"
                await _level(hass, "info")
        finally:
            release.set()
            with pytest.raises(OutcomeUnknown):
                await asyncio.wait_for(command, 2)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("loopback_socket")
async def test_native_trace_retains_240_consecutive_ordinary_polls(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture,
) -> None:
    """One hour of 15-second poll slots survives every retention resource."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    response = _state_response(_standby_words(), frame_type=0x02)
    peers = []
    requests = []
    errors = []

    async def serve(reader, writer):
        try:
            requests.append(await _read_request(reader))
            writer.write(response)
            await writer.drain()
            assert await reader.read() == b""
        except (AssertionError, OSError, ValueError, asyncio.IncompleteReadError) as err:
            errors.append(err)
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader, writer):
        peers.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    async with server:
        entry = _entry()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            entry, data={
                **entry.data, CONF_HOST: "127.0.0.1",
                CONF_PORT: server.sockets[0].getsockname()[1],
            }, pref_disable_polling=True,
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        try:
            await _level(hass, "debug")
            for _ in range(240):
                await coordinator.async_refresh()
                assert coordinator.last_update_success
            await asyncio.wait_for(asyncio.gather(*peers), 5)
            assert not errors
            assert len(requests) == 241  # Initial setup plus the captured hour.
            await _level(hass, "info")
            trace = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert [op["operation_id"] for op in trace["operations"]] == list(range(1, 241))
            assert trace["omitted"] == {
                "operations": 0, "events": 0, "raw_bytes": 0, "metadata": 0,
            }
            for operation in trace["operations"]:
                assert operation["kind"] == "poll"
                assert operation["result"] == {"outcome": "received", "cause": None}
                assert not operation["partial"]
                received = b"".join(
                    bytes.fromhex(event["data_hex"]) for event in operation["events"]
                    if event["event"] == "raw" and event["direction"] == "rx"
                )
                assert received == response
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)
            await asyncio.wait_for(asyncio.gather(*peers), 5)
