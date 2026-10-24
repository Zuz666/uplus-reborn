"""Discovery lifetime through the same interface used by native callers."""

import asyncio
import struct
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from custom_components.uplus_reborn.const import SUPPORTED_WIFI_TYPE
from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval


async def wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class Responder(asyncio.DatagramProtocol):
    """Receive real loopback broadcasts; explicitly release replies in each test."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []
        self.closed = asyncio.Event()
        self.reply_transport: asyncio.DatagramTransport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        assert data[:9] == b"Haier\0\0i\x15"
        self.queries.append(address)

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed.set()

    def reply(self, *, identity: str = "020000000001") -> None:
        body = bytearray(288)
        body[:16] = identity.encode().ljust(16, b"\0")
        body[16:48] = bytes.fromhex(SUPPORTED_WIFI_TYPE)
        body[208:224] = b"127.0.0.2".ljust(16, b"\0")
        packet = b"Haier" + struct.pack(">IH6sI", 0x684D, 0, bytes(6), 288) + body
        self.reply_transport.sendto(packet, self.queries[0])


@pytest.fixture
async def responder(socket_enabled):
    pytest_socket.socket_allow_hosts(
        ["127.0.0.1", "127.0.0.2", "127.255.255.255"], allow_unix_socket=True
    )
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        Responder, local_addr=("127.255.255.255", 7083)
    )
    reply_transport, reply_peer = await loop.create_datagram_endpoint(
        Responder, local_addr=("127.0.0.2", 0)
    )
    peer.reply_transport = reply_transport
    adapters = [{"enabled": True, "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}]}]
    try:
        with patch(
            "custom_components.uplus_reborn.local_discovery.async_get_adapters",
            AsyncMock(return_value=adapters),
        ):
            yield peer
    finally:
        transport.close()
        reply_transport.close()
        await peer.closed.wait()
        await reply_peer.closed.wait()


async def test_only_authorized_subscription_starts_an_interval(hass: HomeAssistant) -> None:
    interval = DiscoveryInterval(hass, "owner")
    with patch(
        "custom_components.uplus_reborn.local_discovery.async_get_adapters",
        AsyncMock(return_value=[]),
    ) as adapters:
        assert interval.progress.sent == 0
        with pytest.raises(ValueError):
            await interval.async_subscribe("another-admin", lambda _: None)
        adapters.assert_not_called()
        await interval.async_subscribe("owner", lambda _: None)
        result = await interval.async_wait()
        assert result.records == ()
        assert result.issues == ("no_enabled_interfaces",)
        assert interval.progress.outcome == "failed"
        with pytest.raises(ValueError):
            await interval.async_subscribe("owner", lambda _: None)
        assert await interval.async_stop() == result


async def test_concurrent_intervals_keep_send_fanout_and_independent_stop(
    hass: HomeAssistant, responder: Responder,
) -> None:
    first = DiscoveryInterval(hass, "first")
    second = DiscoveryInterval(hass, "second")
    first_owner = await first.async_subscribe("first", lambda _: None)
    await second.async_subscribe("second", lambda _: None)
    try:
        await wait_for(lambda: len(responder.queries) == 2)
        responder.reply()
        responder.reply()
        await wait_for(lambda: first.progress.replies == second.progress.replies == 2)
        assert (first.progress.sent, second.progress.sent) == (1, 1)
        assert (first.progress.devices, second.progress.devices) == (1, 1)
        first_owner()
        result = await first.async_wait()
        frozen = first.progress
        assert frozen.outcome == "disconnected"
        assert [record.device_id for record in result.records] == ["020000000001"]
        with pytest.raises(FrozenInstanceError):
            frozen.devices = 42
        responder.reply(identity="020000000002")
        await wait_for(lambda: second.progress.devices == 2)
        assert first.progress == frozen
        assert second.progress.replies == 3
    finally:
        await first.async_stop()
        await second.async_stop()


@pytest.mark.parametrize("ending", ["stop", "close", "shutdown"])
async def test_completion_is_final_and_releases_real_sockets(
    hass: HomeAssistant, responder: Responder, ending: str,
) -> None:
    interval = DiscoveryInterval(hass, "owner")
    published = []
    first = await interval.async_subscribe("owner", published.append)
    last = await interval.async_subscribe("owner", published.append)
    await wait_for(lambda: len(responder.queries) == 1)
    first()
    responder.reply()
    await wait_for(lambda: interval.progress.replies == 1)
    assert interval.progress.outcome == "searching"
    if ending == "stop":
        await interval.async_stop()
        expected = "stopped"
    elif ending == "close":
        interval.close()
        expected = "closed"
    else:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        expected = "shutdown"
    await interval.async_wait()
    frozen = interval.progress
    assert frozen.outcome == expected
    last()
    interval.close()
    assert interval.progress == frozen
    with pytest.raises(ValueError):
        await interval.async_subscribe("owner", published.append)
    await asyncio.sleep(0)
    # Binding the same fixed port proves the final interval released the socket.
    transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        Responder, local_addr=("127.0.0.1", 7083)
    )
    transport.close()
    await peer.closed.wait()


async def test_stopped_before_subscription_never_sends(
    hass: HomeAssistant, responder: Responder,
) -> None:
    interval = DiscoveryInterval(hass, "owner")
    result = await interval.async_stop()
    with pytest.raises(ValueError):
        await interval.async_subscribe("owner", lambda _: None)
    assert result.records == ()
    assert interval.progress.outcome == "stopped"
    assert responder.queries == []


async def test_cancelling_a_waiter_does_not_cancel_owned_collection(
    hass: HomeAssistant, responder: Responder,
) -> None:
    interval = DiscoveryInterval(hass, "owner")
    await interval.async_subscribe("owner", lambda _: None)
    waiter = asyncio.create_task(interval.async_wait())
    await wait_for(lambda: len(responder.queries) == 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    responder.reply()
    await wait_for(lambda: interval.progress.replies == 1)
    assert interval.progress.outcome == "searching"
    result = await interval.async_stop()
    assert [item.device_id for item in result.records] == ["020000000001"]


async def test_old_completion_and_unsubscribe_cannot_change_new_interval(
    hass: HomeAssistant, responder: Responder,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def adapters(_hass):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return [{"enabled": True, "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}]}]

    old = DiscoveryInterval(hass, "owner")
    new = DiscoveryInterval(hass, "owner")
    with patch(
        "custom_components.uplus_reborn.local_discovery.async_get_adapters", adapters
    ):
        unsubscribe = await old.async_subscribe("owner", lambda _: None)
        await entered.wait()
        unsubscribe()
        frozen = old.progress
        await new.async_subscribe("owner", lambda _: None)
        release.set()
        await old.async_wait()
        await wait_for(lambda: len(responder.queries) == 1)
        unsubscribe()
        responder.reply()
        await wait_for(lambda: new.progress.replies == 1)
        assert old.progress == frozen
        assert new.progress.outcome == "searching"
        assert (await new.async_stop()).records[0].device_id == "020000000001"


async def test_setup_failure_publishes_terminal_observations_once(
    hass: HomeAssistant,
) -> None:
    interval = DiscoveryInterval(hass, "owner")
    published = []
    with patch(
        "custom_components.uplus_reborn.local_discovery.async_get_adapters",
        AsyncMock(side_effect=OSError("socket setup unavailable")),
    ):
        unsubscribe = await interval.async_subscribe("owner", published.append)
        result = await interval.async_wait()
        await wait_for(lambda: bool(published) and published[-1].outcome == "failed")
    frozen = interval.progress
    assert result.issues == ("scan_failed",)
    assert frozen.issues == result.issues
    unsubscribe()
    await interval.async_stop()
    assert interval.progress == frozen


async def test_late_join_observes_only_its_own_window(
    hass: HomeAssistant, responder: Responder,
) -> None:
    first = DiscoveryInterval(hass, "owner")
    second = DiscoveryInterval(hass, "owner")
    await first.async_subscribe("owner", lambda _: None)
    try:
        await wait_for(lambda: len(responder.queries) == 1)
        responder.reply()
        await wait_for(lambda: first.progress.replies == 1)
        await second.async_subscribe("owner", lambda _: None)
        await wait_for(lambda: len(responder.queries) == 2)
        responder.reply()
        await wait_for(lambda: second.progress.replies == 1)
        assert (first.progress.replies, second.progress.replies) == (2, 1)
        first.close()
        await first.async_wait()
        responder.reply()
        await wait_for(lambda: second.progress.replies == 2)
        assert first.progress.replies == 2
        assert second.progress.outcome == "searching"
    finally:
        await first.async_stop()
        await second.async_stop()


async def test_bounded_journal_keeps_coverage_issues_and_exact_counters(
    hass: HomeAssistant, responder: Responder,
) -> None:
    occupied, occupied_peer = await asyncio.get_running_loop().create_datagram_endpoint(
        Responder, local_addr=("127.0.0.1", 7083)
    )
    interval = DiscoveryInterval(hass, "owner")
    try:
        await interval.async_subscribe("owner", lambda _: None)
        await wait_for(lambda: len(responder.queries) == 1)
        for number in range(20):
            responder.reply(identity=f"0200000000{number:02X}")
        await wait_for(lambda: interval.progress.replies == 20)
        await interval.async_stop()
        state = interval.progress
        assert state.issues == ("fixed_port_unavailable",)
        assert (state.replies, state.devices) == (20, 20)
        assert len(state.events) == 12
    finally:
        await interval.async_stop()
        occupied.close()
        await occupied_peer.closed.wait()
