"""Native DEBUG lifecycle and downloaded trace consumer contracts."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
    await _level(hass, "info")
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
            if len(connections) == 3:
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
        if enabled:
            await _level(hass, "debug")
        await coordinator.async_refresh()
        coordinator.stage_program("upper", "spin")
        coordinator.stage_settings("upper", spin_rpm=400)
        result = await coordinator.async_start_program("upper")
        assert result.after.upper.phase_value == 0x0B
        await asyncio.wait_for(asyncio.gather(*peer_tasks), 2)
        assert not errors
        assert closed == [True, True, True]
        assert [len(requests) for requests in connections] == [1, 1, 3]
        assert [sequence for sequence, _ in connections[-1]] == [0, 1, 2]
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
            assert active["partial_operations"] == 1
            await _level(hass, "info")
            stopped = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert stopped["operations"][-1]["partial"]
            status = coordinator.capture_status
            assert stopped["partial_operations"] == 1
            assert status["stop_reason"] == "debug_disabled"
            assert status["stopped_at"] is not None
            assert status["debug_enabled"] is False
            assert stopped["last_recorded_at"] == active["last_recorded_at"]
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


async def test_setup_and_reload_do_not_resume_capture_under_existing_debug(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Saved/effective logging is not fresh consent after a coordinator restart."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "debug")
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        try:
            assert await hass.config_entries.async_setup(entry.entry_id)
            assert not entry.runtime_data.trace.active
            await _level(hass, "info")
            await _level(hass, "debug")
            assert entry.runtime_data.trace.active
            old = entry.runtime_data
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert not entry.runtime_data.trace.active
            assert old.trace.export()["capture_state"] == "closed"
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("duration", [1800, 3600, 7200, 14400, 21600])
async def test_native_capture_deadline_freezes_evidence_without_disabling_user_debug(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, freezer, duration: int,
) -> None:
    """A native start has a finite deadline; elapsed time never consumes evidence."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "diagnostic_duration": duration},
        pref_disable_polling=True,
    )
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await _level(hass, "debug")
            active_status = entry.runtime_data.capture_status
            assert active_status["stopped_at"] is None
            assert active_status["stop_reason"] is None
            assert active_status["debug_enabled"] is True
            assert active_status["first_retained_at"] is None
            assert active_status["last_recorded_at"] is None
            with pytest.raises(UnsafeState):
                await entry.runtime_data.async_start_program("upper")
            freezer.tick(timedelta(seconds=duration - 1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert entry.runtime_data.trace.active
            freezer.tick(timedelta(seconds=2))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            frozen = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert frozen["capture_state"] == "stopped"
            assert frozen["operations"][0]["result"]["cause"] == "program_not_staged"
            assert entry.runtime_data.capture_status["remaining_seconds"] == 0
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
            status = entry.runtime_data.capture_status
            assert status["stopped_at"] is not None
            assert status["stopped_at"] != status["end_at"]
            assert status["stop_reason"] == "deadline"
            assert status["debug_enabled"] is True
            assert status["first_retained_at"] == frozen["operations"][0]["started_at"]
            assert status["last_recorded_at"] == frozen["operations"][0]["finished_at"]
            await _level(hass, "debug")
            assert not entry.runtime_data.trace.active
            freezer.tick(timedelta(seconds=10))
            entry.runtime_data.capture.stop()
            await _level(hass, "info")
            repeated = entry.runtime_data.capture_status
            assert repeated["stopped_at"] == status["stopped_at"]
            assert repeated["stop_reason"] == "deadline"
            assert repeated["debug_enabled"] is False
            await _level(hass, "debug")
            replacement = entry.runtime_data.capture_status
            assert replacement["stopped_at"] is None
            assert replacement["stop_reason"] is None
            assert replacement["first_retained_at"] is None
            assert replacement["last_recorded_at"] is None
            await _level(hass, "info")
            frozen = (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"]
            assert not entry.runtime_data.trace.active
            assert (await async_get_config_entry_diagnostics(hass, entry))["exchange_trace"] == frozen
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


async def _save_capture(
    hass: HomeAssistant, entry, *, start: bool = True,
    diagnostics: dict | None = None, controls: bool | None = None,
) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    values = {"diagnostics": {**(diagnostics or {}), "start_capture": start}}
    if controls is not None:
        values["enable_verified_controls"] = controls
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )


async def test_options_captures_isolate_deadlines_and_restore_only_owned_logging(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, freezer,
) -> None:
    """Replacement cancels old expiry; only the last owned capture restores DEBUG."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entries = [_entry(controls=True), _entry(controls=True, device_id="AABBCCDDEE02")]
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        for entry in entries:
            entry.add_to_hass(hass)
            hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
            assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            first, second = entries
            await _save_capture(hass, first, diagnostics={"diagnostic_duration": "1800"})
            assert first.runtime_data.trace.active
            assert not second.runtime_data.trace.active
            await _save_capture(hass, second, diagnostics={"diagnostic_duration": "7200"})
            with pytest.raises(UnsafeState):
                await first.runtime_data.async_start_program("upper")
            freezer.tick(timedelta(seconds=1799))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            await _save_capture(hass, first, diagnostics={"diagnostic_duration": "3600"})
            assert first.runtime_data.trace.export()["operations"] == []
            freezer.tick(timedelta(seconds=2))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert first.runtime_data.trace.active
            assert second.runtime_data.trace.active
            freezer.tick(timedelta(seconds=3600))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert not first.runtime_data.trace.active
            assert second.runtime_data.trace.active
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
            first_stop = first.runtime_data.capture_status
            assert first_stop["stop_reason"] == "deadline"
            assert first_stop["debug_enabled"] is True
            freezer.tick(timedelta(seconds=1800))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert not second.runtime_data.trace.active
            assert not logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
            assert first.runtime_data.capture_status["stopped_at"] == first_stop["stopped_at"]
            assert first.runtime_data.capture_status["stop_reason"] == "deadline"
            assert second.runtime_data.capture_status["stop_reason"] == "deadline"
            assert second.runtime_data.capture_status["debug_enabled"] is False
        finally:
            await _level(hass, "info")
            for entry in entries:
                assert await hass.config_entries.async_unload(entry.entry_id)


async def test_checked_start_survives_same_save_control_reload(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Volatile consent is delivered to the resulting coordinator exactly once."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        old = entry.runtime_data
        try:
            result = await _save_capture(
                hass, entry, controls=True,
                diagnostics={"diagnostic_operations": 2, "diagnostic_duration": "1800"},
            )
            await hass.async_block_till_done()
            assert result["type"] == "create_entry"
            assert entry.runtime_data is not old
            assert old.trace.export()["capture_state"] == "closed"
            assert entry.runtime_data.trace.active
            assert entry.runtime_data.capture_status["limits"]["operations"] == 2
            assert entry.runtime_data.capture_status["remaining_seconds"] <= 1800
            with pytest.raises(UnsafeState):
                await entry.runtime_data.async_start_program("upper")
            assert len(entry.runtime_data.trace.export()["operations"]) == 1
            assert "start_capture" not in entry.options
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert not entry.runtime_data.trace.active
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_start_enables_native_menu_without_owning_existing_package_debug(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, freezer,
) -> None:
    """Native Disable stays available while a separately enabled logger is preserved."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    native_logger = "homeassistant.components.uplus_reborn"
    await _level(hass, "warning", native_logger)
    await _level(hass, "debug")
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await _save_capture(hass, entry, diagnostics={"diagnostic_duration": "1800"})
            assert logging.getLogger(native_logger).getEffectiveLevel() == logging.DEBUG
            assert entry.runtime_data.trace.active
            freezer.tick(timedelta(seconds=1801))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert not entry.runtime_data.trace.active
            assert logging.getLogger(native_logger).getEffectiveLevel() == logging.WARNING
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("inherited_debug", [False, True])
async def test_native_disable_cancels_checked_start_waiting_for_reload(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, inherited_debug: bool,
) -> None:
    """Stop is newer than consent, including the zero-loaded-entry reload gap."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    previous_default = logging.getLogger().level
    await hass.services.async_call(
        "logger", "set_default_level",
        {"level": "debug" if inherited_debug else "info"}, blocking=True,
    )
    native_logger = "homeassistant.components.uplus_reborn"
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.services.async_call(
        "logger", "set_level",
        {LOGGER: "notset" if inherited_debug else "debug",
         native_logger: "notset" if inherited_debug else "debug"},
        blocking=True,
    )
    await hass.async_block_till_done()
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_read():
        entered.set()
        await release.wait()
        return standby_snapshot

    with patch.object(UwtClient, "read_state", AsyncMock(side_effect=first_read)):
        save = asyncio.create_task(_save_capture(hass, entry, controls=True))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await hass.services.async_call(
                "logger", "set_level",
                {LOGGER: "notset", native_logger: "notset"}, blocking=True,
            )
            await hass.async_block_till_done()
            release.set()
            result = await asyncio.wait_for(save, 5)
            assert result["type"] == "form"
            assert result["errors"] == {"base": "capture_start_failed"}
            assert not entry.runtime_data.trace.active
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG) is inherited_debug
            hass.config_entries.options.async_abort(result["flow_id"])
        finally:
            release.set()
            if not save.done():
                await save
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.services.async_call(
                "logger", "set_default_level",
                {"level": logging.getLevelName(previous_default).lower()}, blocking=True,
            )


async def test_reload_start_keeps_confirmed_budgets_without_overwriting_newer_preferences(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Concurrent preference-only Save is next-capture policy, not older consent."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_read():
        entered.set()
        await release.wait()
        return standby_snapshot

    with patch.object(UwtClient, "read_state", AsyncMock(side_effect=first_read)):
        save = asyncio.create_task(_save_capture(
            hass, entry, controls=True,
            diagnostics={"diagnostic_operations": 2, "diagnostic_duration": "1800"},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await _save_capture(hass, entry, start=False, diagnostics={
                "diagnostic_operations": 3, "diagnostic_duration": "7200",
            })
            release.set()
            result = await asyncio.wait_for(save, 5)
            assert result["type"] == "create_entry"
            assert entry.runtime_data.capture_status["limits"]["operations"] == 2
            assert 0 < entry.runtime_data.capture_status["remaining_seconds"] <= 1800
            assert entry.options["diagnostic_operations"] == 3
            assert entry.options["diagnostic_duration"] == 7200
        finally:
            release.set()
            if not save.done():
                await save
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_shutdown_closes_capture_without_listener_exception(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An already-consumed one-shot callback is not removed a second time."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    caplog.clear()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert coordinator.trace.export()["capture_state"] == "closed"
    assert not [record for record in caplog.records if record.exc_info]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("external_logger", [LOGGER, "unrelated.integration"])
async def test_ambiguous_external_logging_change_relinquishes_restore_not_capture(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, freezer, external_logger: str,
) -> None:
    """Missing logger provenance must not let timer cleanup override a user choice."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await _save_capture(hass, entry, diagnostics={"diagnostic_duration": "1800"})
            deadline = entry.runtime_data.capture_status["end_at"]
            await _level(hass, "debug", external_logger)
            assert entry.runtime_data.trace.active
            assert entry.runtime_data.capture_status["end_at"] == deadline
            freezer.tick(timedelta(seconds=1801))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert not entry.runtime_data.trace.active
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
        finally:
            await _level(hass, "info")
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_routing_update_during_capture_reload_is_reconciled(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pending capture cannot suppress DHCP's newer connection data at setup."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    first, second = _entry(), _entry(device_id="AABBCCDDEE02")
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        for entry in (first, second):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_read(client):
        if client.device_id == first.unique_id and not entered.is_set():
            entered.set()
            await release.wait()
        return standby_snapshot

    with patch.object(UwtClient, "read_state", first_read):
        save = asyncio.create_task(_save_capture(hass, first, controls=True))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            hass.config_entries.async_update_entry(
                first, data={**first.data, CONF_HOST: "192.0.2.11"}
            )
            release.set()
            result = await asyncio.wait_for(save, 5)
            await hass.async_block_till_done()
            assert first.runtime_data.client.host == "192.0.2.11"
            if result["type"] == "form":
                assert result["errors"] == {"base": "capture_start_failed"}
                hass.config_entries.options.async_abort(result["flow_id"])
            else:
                assert first.runtime_data.trace.active
        finally:
            release.set()
            if not save.done():
                await save
            await _level(hass, "info")
            for entry in (first, second):
                assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_notset_stops_capture_when_debug_is_inherited_from_root(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clearing native overrides is still a stop when DEBUG remains inherited."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    previous_default = logging.getLogger().level
    await hass.services.async_call(
        "logger", "set_default_level", {"level": "info"}, blocking=True
    )
    native_logger = "homeassistant.components.uplus_reborn"
    await hass.services.async_call(
        "logger", "set_level", {LOGGER: "notset", native_logger: "notset"}, blocking=True
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await hass.services.async_call(
                "logger", "set_default_level", {"level": "debug"}, blocking=True
            )
            await hass.async_block_till_done()
            assert not entry.runtime_data.trace.active
            await _save_capture(hass, entry)
            await hass.services.async_call(
                "logger", "set_level",
                {LOGGER: "notset", native_logger: "notset"}, blocking=True,
            )
            await hass.async_block_till_done()
            assert logging.getLogger(LOGGER).isEnabledFor(logging.DEBUG)
            assert not entry.runtime_data.trace.active
            assert entry.runtime_data.capture_status["stop_reason"] == "debug_disabled"
            assert entry.runtime_data.capture_status["debug_enabled"] is True
            await hass.services.async_call(
                "logger", "set_level",
                {LOGGER: "debug", native_logger: "debug"}, blocking=True,
            )
            await hass.async_block_till_done()
            assert entry.runtime_data.trace.active
        finally:
            await _level(hass, "info")
            await hass.services.async_call(
                "logger", "set_default_level",
                {"level": logging.getLevelName(previous_default).lower()}, blocking=True,
            )
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_disabling_entry_during_checked_reload_returns_truthful_start_error(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
) -> None:
    """A successful unload with disabled replacement is not successful setup."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        old = entry.runtime_data
        entered, release = asyncio.Event(), asyncio.Event()
        unload = hass.config_entries.async_unload_platforms

        async def delayed_unload(current, platforms):
            if current is entry and not entered.is_set():
                entered.set()
                await release.wait()
            return await unload(current, platforms)

        with patch.object(hass.config_entries, "async_unload_platforms", delayed_unload):
            save = asyncio.create_task(_save_capture(hass, entry, controls=True))
            disable = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                disable = asyncio.create_task(hass.config_entries.async_set_disabled_by(
                    entry.entry_id, ConfigEntryDisabler.USER,
                ))
                async with asyncio.timeout(5):
                    while entry.disabled_by is not ConfigEntryDisabler.USER:
                        await asyncio.sleep(0)
                release.set()
                result = await asyncio.wait_for(save, 5)
                assert await asyncio.wait_for(disable, 5)
                assert result["type"] == "form"
                assert result["errors"] == {"base": "capture_start_failed"}
                assert entry.disabled_by is ConfigEntryDisabler.USER
                assert entry.state.value == "not_loaded"
                assert old.trace.export()["capture_state"] == "closed"
                hass.config_entries.options.async_abort(result["flow_id"])
            finally:
                release.set()
                await asyncio.gather(save, *([disable] if disable is not None else []))


async def test_stale_deadline_callbacks_cannot_rewrite_replaced_or_closed_capture(
    hass: HomeAssistant, standby_snapshot: DeviceSnapshot,
    caplog: pytest.LogCaptureFixture, freezer,
) -> None:
    """Even a delivered cancelled callback cannot stop a replacement or keep an archive."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await async_setup_component(hass, "logger", {})
    await _level(hass, "info")
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    with patch.object(UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    try:
        with patch(
            "custom_components.uplus_reborn.capture_lifecycle.async_call_later",
            return_value=lambda: None,
        ) as timer:
            await _save_capture(hass, entry)
            old_expiry = timer.call_args.args[2]
            freezer.tick(timedelta(seconds=30))
            await _save_capture(hass, entry)
            replacement_expiry = timer.call_args.args[2]
            replaced = coordinator.capture_status
            old_expiry(None)
            assert coordinator.trace.active
            assert coordinator.capture_status["started_at"] == replaced["started_at"]
            assert coordinator.capture_status["stopped_at"] is None
            assert coordinator.capture_status["stop_reason"] is None
            await _level(hass, "info")
            stopped = coordinator.capture_status
            freezer.tick(timedelta(seconds=30))
            replacement_expiry(None)
            assert coordinator.capture_status["stopped_at"] == stopped["stopped_at"]
            assert coordinator.capture_status["stop_reason"] == "debug_disabled"
            assert await hass.config_entries.async_unload(entry.entry_id)
            old_expiry(None)
            replacement_expiry(None)
            closed = coordinator.capture_status
            assert closed["capture_state"] == "closed"
            for key in (
                "started_at", "end_at", "stopped_at", "stop_reason",
                "remaining_seconds", "first_retained_at", "last_recorded_at",
            ):
                assert closed[key] is None
            assert coordinator.trace.export()["operations"] == []
    finally:
        await _level(hass, "info")
        if coordinator.trace.summary()["capture_state"] != "closed":
            assert await hass.config_entries.async_unload(entry.entry_id)
