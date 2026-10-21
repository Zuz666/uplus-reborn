"""Pause/Resume rejection UX through HA and real loopback UWT sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplus_reborn.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    DOMAIN,
)
from custom_components.uplus_reborn.protocol._protocol import _decode_inner_frame

from .conftest import DEVICE_ID
from .test_protocol import _cycle_words, _frame_response, _read_request, _state_response


@dataclass
class LifecyclePeer:
    """A synthetic appliance with observable controls and independently changed state."""

    port: int = 0
    words: list[int] = field(default_factory=lambda: _cycle_words("upper"))
    controls: list[str] = field(default_factory=list)
    queries: int = 0
    hold_query: asyncio.Event | None = None
    query_started: asyncio.Event = field(default_factory=asyncio.Event)
    failure: str | None = None

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                sequence, raw = await _read_request(reader)
                frame, _ = _decode_inner_frame(raw)
                if frame.frame_type == 0x01 and frame.payload == b"\x4d\x01":
                    self.queries += 1
                    if self.hold_query is not None:
                        gate, self.hold_query = self.hold_query, None
                        self.query_started.set()
                        await gate.wait()
                    if self.failure == "closed":
                        return
                    response = _state_response(
                        self.words, sequence=sequence, frame_type=0x02
                    )
                    if self.failure == "malformed":
                        response = response[:-1] + bytes((response[-1] ^ 1,))
                    writer.write(response)
                elif frame.frame_type == 0x73:
                    writer.write(
                        _frame_response(
                            0x74,
                            bytes.fromhex("0f5a0000000000000000000000000000"),
                            sequence=sequence,
                        )
                    )
                else:
                    opcode = frame.payload.hex()
                    self.controls.append(opcode)
                    phase_word = 3 if opcode in ("4d05", "4d04") else 26
                    if opcode in ("4d05", "4d85"):
                        self.words[phase_word] &= ~0x80
                    elif opcode in ("4d04", "4d84"):
                        self.words[phase_word] |= 0x80
                    if self.failure == "after_control":
                        return
                    writer.write(_frame_response(0x03, b"\x00\x00", sequence=sequence))
                    writer.write(_state_response(self.words))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture
async def lifecycle_peer(loopback_socket: None) -> AsyncIterator[LifecyclePeer]:
    peer = LifecyclePeer()
    async with await asyncio.start_server(peer.handle, "127.0.0.1", 0) as server:
        peer.port = server.sockets[0].getsockname()[1]
        yield peer


def _entry(peer: LifecyclePeer) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="HWD120-B1558U",
        data={"host": "127.0.0.1", "port": peer.port, CONF_DEVICE_ID: DEVICE_ID},
        options={CONF_ENABLE_VERIFIED_CONTROLS: True},
        unique_id=DEVICE_ID,
    )


def _id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{DEVICE_ID}_{key}"
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.parametrize("drum", ["upper", "lower"])
async def test_panel_change_before_pause_returns_state_changed_without_write(
    hass: HomeAssistant, lifecycle_peer: LifecyclePeer, drum: str
) -> None:
    """A stale enabled button cannot pause a newly paused drum or mislabel the race."""
    peer = lifecycle_peer
    peer.words = _cycle_words(drum)
    entry = _entry(peer)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    peer.words[3 if drum == "upper" else 26] &= ~0x80
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: _id(hass, "button", f"{drum}_pause")},
            blocking=True,
        )
    assert raised.value.translation_key == "pause_resume_state_changed"
    assert raised.value.translation_placeholders is None
    assert peer.controls == []
    assert entry.runtime_data.health.latest_command.outcome == "not_dispatched"


@pytest.mark.parametrize("drum", ["upper", "lower"])
@pytest.mark.parametrize(
    ("action", "change", "reason"),
    [
        ("pause", "paused", "not_active"),
        ("resume", "running", "not_paused"),
        ("pause", "completed", "unsupported_phase"),
        ("pause", "unsupported", "unsupported_phase"),
        ("pause", "remote_off", "remote_control_off"),
        ("pause", "power_off", "power_off"),
        ("pause", "coupled_lock", "local_lock"),
        ("pause", "child_lock", "child_lock"),
        ("pause", "opt_out", "controls_disabled"),
    ],
)
async def test_queued_lifecycle_request_reports_actual_rejection(
    hass: HomeAssistant,
    lifecycle_peer: LifecyclePeer,
    drum: str,
    action: str,
    change: str,
    reason: str,
) -> None:
    """A poll finishing ahead of a queued press preserves gates and structured cause."""
    peer = lifecycle_peer
    peer.words = _cycle_words(drum, active=action == "pause")
    entry = _entry(peer)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    button_id = _id(hass, "button", f"{drum}_{action}")
    pressed = asyncio.Event()

    def on_press(event: Event) -> None:
        if event.data["entity_id"] == button_id:
            pressed.set()

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, on_press)
    release = asyncio.Event()
    peer.hold_query = release
    poll = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(peer.query_started.wait(), 1)
    command = asyncio.create_task(
        hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: button_id}, blocking=True
        )
    )
    try:
        await asyncio.wait_for(pressed.wait(), 1)
        phase_word = 3 if drum == "upper" else 26
        if change == "paused":
            peer.words[phase_word] &= ~0x80
        elif change == "running":
            peer.words[phase_word] |= 0x80
        elif change in ("completed", "unsupported"):
            phase = 0x13 if change == "completed" else 0xFE
            peer.words[phase_word] = phase << 8 | 0x23
        elif change == "remote_off":
            peer.words[3] &= ~0x02
        elif change == "power_off":
            peer.words[3] &= ~0x01
        elif change == "coupled_lock":
            peer.words[3] |= 0x08
            peer.words[2] |= 0x08
        elif change == "child_lock":
            peer.words[2] |= 0x08
        else:
            hass.config_entries.async_update_entry(
                entry, options={CONF_ENABLE_VERIFIED_CONTROLS: False}
            )
        release.set()
        await poll
        with pytest.raises(ServiceValidationError) as raised:
            await command
        assert raised.value.translation_key == f"pause_resume_{reason}"
        assert raised.value.translation_placeholders is None
        assert peer.controls == []
        assert coordinator.health.latest_command.outcome == "not_dispatched"
        assert coordinator.health.latest_command.cause == reason
    finally:
        unsubscribe()
        release.set()
        await asyncio.gather(poll, command, return_exceptions=True)


@pytest.mark.parametrize("drum", ["upper", "lower"])
@pytest.mark.parametrize("failure", ["closed", "malformed", "after_control"])
async def test_lifecycle_connection_failure_keeps_unknown_outcome_distinct(
    hass: HomeAssistant, lifecycle_peer: LifecyclePeer, drum: str, failure: str
) -> None:
    """Pre-write failures and unproven sent commands differ; neither retries controls."""
    peer = lifecycle_peer
    peer.words = _cycle_words(drum)
    entry = _entry(peer)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    peer.failure = failure
    queries = peer.queries
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: _id(hass, "button", f"{drum}_pause")},
            blocking=True,
        )
    sent = failure == "after_control"
    assert raised.value.translation_key == (
        "outcome_unknown" if sent else "pause_resume_control_failed"
    )
    assert raised.value.translation_placeholders is None
    assert peer.controls == (
        ["4d05" if drum == "upper" else "4d85"] if sent else []
    )
    assert peer.queries == queries + 1
    expected_outcome = "unknown" if sent else "not_dispatched"
    assert entry.runtime_data.health.latest_command.outcome == expected_outcome
    await hass.async_block_till_done()
    state_id = _id(hass, "sensor", f"{drum}_execution_state")
    assert hass.states.get(state_id).state == STATE_UNAVAILABLE
    peer.failure = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == ("paused" if sent else "running")
    assert entry.runtime_data.health.latest_command.outcome == expected_outcome


async def test_explicit_lifecycle_targets_preserve_independent_confirmed_states(
    hass: HomeAssistant, lifecycle_peer: LifecyclePeer
) -> None:
    """Pause/Resume remain explicit, gated and confirmed; invalid repeated calls skip."""
    peer = lifecycle_peer
    peer.words = _cycle_words("upper")
    lower = _cycle_words("lower", active=False)
    peer.words[24:] = lower[24:]
    entry = _entry(peer)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for drum, action in (
        ("upper", "pause"),
        ("lower", "resume"),
        ("upper", "resume"),
        ("lower", "pause"),
    ):
        button_id = _id(hass, "button", f"{drum}_{action}")
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: button_id}, blocking=True
        )
        await hass.async_block_till_done()
        state_id = _id(hass, "sensor", f"{drum}_execution_state")
        assert hass.states.get(state_id).state == (
            "paused" if action == "pause" else "running"
        )
        assert hass.states.get(button_id).state == STATE_UNAVAILABLE
        queries = peer.queries
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: button_id}, blocking=True
        )
        assert peer.queries == queries
    assert peer.controls == ["4d05", "4d84", "4d04", "4d85"]
    upper_pause = _id(hass, "button", "upper_pause")
    upper_resume = _id(hass, "button", "upper_resume")
    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: [upper_pause, upper_resume]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert peer.controls == ["4d05", "4d84", "4d04", "4d85", "4d05"]
    assert hass.states.get(_id(hass, "sensor", "upper_execution_state")).state == "paused"
    assert hass.states.get(_id(hass, "sensor", "lower_execution_state")).state == "paused"
