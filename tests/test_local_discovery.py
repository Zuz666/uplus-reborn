"""Consumer-visible LAN discovery parser, lifetime and onboarding contracts."""

from __future__ import annotations

import asyncio
import gc
import struct
import weakref
from dataclasses import replace
from ipaddress import IPv4Network
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from homeassistant.config_entries import SOURCE_USER, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplus_reborn.const import DOMAIN, SUPPORTED_WIFI_TYPE
from custom_components.uplus_reborn.discovery_preview import DISCOVERY_INTERVALS
from custom_components.uplus_reborn.local_discovery import (
    QUERY,
    DiscoveryCollector,
    DiscoveryRecord,
    ScanActivity,
    ScanResult,
    async_get_collector,
    decode_record,
)

NETWORKS = (IPv4Network("192.0.2.0/24"),)
ID = "020000000001"
HOST = "192.0.2.10"


@pytest.fixture(autouse=True)
async def _preview_http(hass: HomeAssistant):
    """Initialize HA's actual static-image and preview registration APIs."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})


class Sender:
    def sendto(self, data, address):
        pass


async def open_synthetic(collector: DiscoveryCollector) -> None:
    collector._networks = NETWORKS
    collector._senders = [(Sender(), HOST)]



class RecordingSender(Sender):
    """Expose UDP submissions and controllable transport failures."""

    def __init__(self) -> None:
        self.attempts = 0
        self.submissions: list[tuple[bytes, tuple[str, int]]] = []
        self.error: Exception | None = None
        self.closed = False

    def sendto(self, data, address):
        self.attempts += 1
        if self.error is not None:
            raise self.error
        self.submissions.append((data, address))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def shared_rounds():
    """Release only discovery's next 20-second delay, never unrelated sleeps."""
    actual_sleep = asyncio.sleep
    pending: asyncio.Queue[asyncio.Event] = asyncio.Queue()

    async def sleep(delay, *args, **kwargs):
        if delay != 20:
            return await actual_sleep(delay, *args, **kwargs)
        release = asyncio.Event()
        pending.put_nowait(release)
        await release.wait()

    with patch("custom_components.uplus_reborn.local_discovery.asyncio.sleep", sleep):
        yield pending


def packet(
    *,
    identity: str = ID,
    type_id: str = SUPPORTED_WIFI_TYPE,
    host: str = HOST,
    flags: int = 0,
) -> bytes:
    body = bytearray(288)
    body[:16] = identity.encode().ljust(16, b"\0")
    body[16:48] = bytes.fromhex(type_id)
    body[208:224] = host.encode().ljust(16, b"\0")
    return b"Haier" + struct.pack(">IH6sI", 0x684D, flags, bytes(6), len(body)) + body


def record(**changes: object) -> DiscoveryRecord:
    original = decode_record(packet(), HOST, NETWORKS)
    assert original is not None
    return replace(original, **changes)


@pytest.mark.parametrize(
    "payload,source",
    [
        (packet()[:-1], HOST),
        (packet() + b"\0", HOST),
        (b"Wrong" + packet()[5:], HOST),
        (packet(flags=16), HOST),
        (packet(), "192.0.2.11"),
        (packet(host="198.51.100.1"), "198.51.100.1"),
        (packet(identity="<script>"), HOST),
    ],
)
def test_rejects_malformed_and_untrusted_routing(payload: bytes, source: str) -> None:
    assert decode_record(payload, source, NETWORKS) is None


def test_unknown_type_is_not_dropped_or_supported() -> None:
    result = decode_record(packet(type_id="01" * 32, flags=11), HOST, NETWORKS)
    assert result is not None
    assert not result.supported
    assert result.busy and result.ready_to_bind and result.security == 1
    assert result.type_id == "01" * 32


async def test_deduplication_preserves_devices_and_conflicts(hass: HomeAssistant) -> None:
    collector = DiscoveryCollector(hass)
    activity = ScanActivity()
    with patch.object(collector, "_open", lambda: open_synthetic(collector)):
        scan = asyncio.create_task(collector.scan(activity=activity))
        await asyncio.sleep(0)
        for _ in range(10):
            collector.receive(packet(), HOST)
        collector.receive(packet(identity="020000000002", host="192.0.2.11"), "192.0.2.11")
        collector.receive(packet(type_id="01" * 32), HOST)
        collector.receive(packet(), HOST)  # No last-packet-wins repair.
        collector.receive(packet(identity="020000000003"), HOST)
        activity.stop.set()
        result = await scan
    observations = {item.device_id: item for item in result.records}
    assert set(observations) == {ID, "020000000002", "020000000003"}
    assert observations[ID].conflict
    assert observations["020000000003"].conflict


async def test_real_udp_ephemeral_reply_and_port_occupancy(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A real loopback peer answers the sending port, not the SDK fixed port."""
    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.2"], allow_unix_socket=True)
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.DatagramTransport) -> None:
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            received.append(data)
            self.transport.sendto(packet(host="127.0.0.2"), addr)

    peer, _ = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.2", 7083))
    collector = DiscoveryCollector(hass)
    original_open = collector._open
    activity = ScanActivity()

    async def open_loopback() -> None:
        # Real endpoint creation, occupied fixed port, and production receiver.
        adapters = [
            {"enabled": True, "ipv4": [{"address": "127.0.0.2", "network_prefix": 8}]}
        ]
        with patch(
            "custom_components.uplus_reborn.local_discovery.async_get_adapters",
            AsyncMock(return_value=adapters),
        ):
            await original_open()
        collector._senders[:] = [
            (transport, "127.0.0.2") for transport, _ in collector._senders
        ]

    actual_sleep = asyncio.sleep

    async def quick_sleep(_: float) -> None:
        await actual_sleep(0.02)
        if len(received) >= 3:
            activity.stop.set()

    try:
        with (
            patch.object(collector, "_open", open_loopback),
            patch(
                "custom_components.uplus_reborn.local_discovery.asyncio.sleep",
                quick_sleep,
            ),
        ):
            result = await collector.scan(activity=activity)
        assert received == [QUERY, QUERY, QUERY]
        assert [item.device_id for item in result.records] == [ID]
        assert "fixed_port_unavailable" in result.issues
        state = activity.snapshot()
        assert (state.rounds, state.sent, state.replies, state.devices) == (3, 3, 3, 1)
        assert state.outcome == "stopped"
    finally:
        peer.close()


async def test_partial_udp_opening_and_receive_errors_preserve_shared_search(
    hass: HomeAssistant, shared_rounds,
) -> None:
    from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

    first = DiscoveryInterval(hass, "owner")
    late = DiscoveryInterval(hass, "owner")
    endpoints = {}
    adapters = [{
        "enabled": True,
        "ipv4": [
            {"address": HOST, "network_prefix": 24},
            {"address": "192.0.2.11", "network_prefix": 24},
        ],
    }]

    async def open_endpoint(factory, *, local_addr, **kwargs):
        if local_addr in ((HOST, 7083), ("192.0.2.11", 0)):
            raise OSError("synthetic endpoint unavailable")
        transport, protocol = RecordingSender(), factory()
        endpoints[local_addr] = transport, protocol
        return transport, protocol

    with (
        patch(
            "custom_components.uplus_reborn.local_discovery.async_get_adapters",
            AsyncMock(return_value=adapters),
        ),
        patch.object(
            asyncio.get_running_loop(), "create_datagram_endpoint", open_endpoint
        ),
    ):
        try:
            await first.async_subscribe("owner", lambda _: None)
            next_round = await asyncio.wait_for(shared_rounds.get(), 3)
            sender, receiver = endpoints[(HOST, 0)]
            receiver.error_received(OSError("synthetic receive error"))
            receiver.datagram_received(packet(), (HOST, 45678))
            assert first.progress.outcome == "searching"
            assert (first.progress.rounds, first.progress.sent) == (1, 1)
            assert (first.progress.replies, first.progress.devices) == (1, 1)
            issues = {
                "fixed_port_unavailable", "interface_unavailable", "receive_failed"
            }
            assert set(first.progress.issues) == issues

            await late.async_subscribe("owner", lambda _: None)
            await hass.async_block_till_done()
            assert set(late.progress.issues) == issues
            assert (
                late.progress.rounds, late.progress.sent,
                late.progress.replies, late.progress.devices,
            ) == (0, 0, 0, 0)
            assert sender.submissions == [(QUERY, ("192.0.2.255", 7083))]
            next_round.set()
            await asyncio.wait_for(shared_rounds.get(), 3)
            receiver.datagram_received(packet(), (HOST, 45678))
            assert (first.progress.rounds, first.progress.sent) == (2, 2)
            assert (late.progress.rounds, late.progress.sent) == (1, 1)
            assert (first.progress.replies, late.progress.replies) == (2, 1)
            assert sender.submissions == [(QUERY, ("192.0.2.255", 7083))] * 2
            first_result = await first.async_stop()
            late_result = await late.async_stop()
            assert {item.device_id for item in first_result.records} == {ID}
            assert {item.device_id for item in late_result.records} == {ID}
            assert set(first_result.issues) == set(late_result.issues) == issues
            assert all(transport.closed for transport, _ in endpoints.values())
        finally:
            await first.async_stop()
            await late.async_stop()


async def test_shared_round_counts_each_successful_sender_and_survives_all_send_failures(
    hass: HomeAssistant, shared_rounds,
) -> None:
    from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

    collector = async_get_collector(hass)
    first = DiscoveryInterval(hass, "owner")
    late = DiscoveryInterval(hass, "owner")
    senders = [RecordingSender() for _ in range(3)]
    senders[2].error = OSError("synthetic send error")
    broadcasts = ("192.0.2.255", "198.51.100.255", "203.0.113.255")

    async def open_senders() -> None:
        await open_synthetic(collector)
        collector._senders = list(zip(senders, broadcasts, strict=True))

    with patch.object(collector, "_open", open_senders):
        try:
            await first.async_subscribe("owner", lambda _: None)
            next_round = await asyncio.wait_for(shared_rounds.get(), 3)
            assert (first.progress.rounds, first.progress.sent) == (1, 2)
            assert first.progress.issues == ("send_failed",)
            await late.async_subscribe("owner", lambda _: None)
            await hass.async_block_till_done()
            assert (late.progress.rounds, late.progress.sent) == (0, 0)
            assert late.progress.issues == ("send_failed",)
            assert [sender.attempts for sender in senders] == [1, 1, 1]

            for sender in senders:
                sender.error = OSError("synthetic all-senders failure")
            next_round.set()
            next_round = await asyncio.wait_for(shared_rounds.get(), 3)
            assert (first.progress.rounds, first.progress.sent) == (2, 2)
            assert (late.progress.rounds, late.progress.sent) == (1, 0)
            assert first.progress.outcome == late.progress.outcome == "searching"
            assert [sender.attempts for sender in senders] == [2, 2, 2]

            senders[0].error = senders[1].error = None
            next_round.set()
            await asyncio.wait_for(shared_rounds.get(), 3)
            assert (first.progress.rounds, first.progress.sent) == (3, 4)
            assert (late.progress.rounds, late.progress.sent) == (2, 2)
            assert [sender.attempts for sender in senders] == [3, 3, 3]
            assert senders[0].submissions == [(QUERY, ("192.0.2.255", 7083))] * 2
            assert senders[1].submissions == [(QUERY, ("198.51.100.255", 7083))] * 2
            assert senders[2].submissions == []
            assert (await first.async_stop()).issues == ("send_failed",)
            assert (await late.async_stop()).issues == ("send_failed",)
            assert first.progress.outcome == late.progress.outcome == "stopped"
        finally:
            for sender in senders:
                sender.error = None
            await first.async_stop()
            await late.async_stop()


async def test_fatal_shared_sender_preserves_independent_results_and_explicit_fresh_search(
    hass: HomeAssistant, shared_rounds,
) -> None:
    from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

    first = DiscoveryInterval(hass, "owner")
    second = DiscoveryInterval(hass, "owner")
    empty = DiscoveryInterval(hass, "owner")
    stopped = DiscoveryInterval(hass, "owner")
    fresh = DiscoveryInterval(hass, "owner")
    published = {interval: [] for interval in (first, second, empty)}
    endpoints = {}
    transports = []
    adapters = [{
        "enabled": True,
        "ipv4": [{"address": HOST, "network_prefix": 24}],
    }]

    async def open_endpoint(factory, *, local_addr, **kwargs):
        transport, protocol = RecordingSender(), factory()
        endpoints[local_addr] = transport, protocol
        transports.append(transport)
        return transport, protocol

    with (
        patch(
            "custom_components.uplus_reborn.local_discovery.async_get_adapters",
            AsyncMock(return_value=adapters),
        ),
        patch.object(
            asyncio.get_running_loop(), "create_datagram_endpoint", open_endpoint
        ),
    ):
        try:
            await first.async_subscribe("owner", published[first].append)
            stopped_owner = await stopped.async_subscribe("owner", lambda _: None)
            next_round = await asyncio.wait_for(shared_rounds.get(), 3)
            await hass.async_block_till_done()
            sender, receiver = endpoints[(HOST, 0)]
            receiver.error_received(OSError("synthetic receive error"))
            receiver.datagram_received(packet(), (HOST, 45678))
            stopped_result = await stopped.async_stop()
            stopped_progress = stopped.progress

            second_owner = await second.async_subscribe("owner", published[second].append)
            await hass.async_block_till_done()
            receiver.datagram_received(
                packet(identity="020000000002", host="192.0.2.11"),
                ("192.0.2.11", 45678),
            )
            await empty.async_subscribe("owner", published[empty].append)
            await hass.async_block_till_done()
            expected_records = {
                interval: tuple(
                    event.record for event in interval.progress.events
                    if event.kind == "found"
                )
                for interval in (first, second, empty)
            }
            assert [
                item.device_id for item in expected_records[first]
            ] == [ID, "020000000002"]
            assert [
                item.device_id for item in expected_records[second]
            ] == ["020000000002"]
            assert expected_records[empty] == ()
            assert sender.submissions == [(QUERY, ("192.0.2.255", 7083))]

            sender.error = RuntimeError("synthetic fatal sender termination")
            next_round.set()
            results = await asyncio.wait_for(
                asyncio.gather(*(interval.async_wait() for interval in published)), 3
            )
            await hass.async_block_till_done()
            frozen = {interval: interval.progress for interval in published}
            for interval, result in zip(published, results, strict=True):
                assert result.records == expected_records[interval]
                assert set(result.issues) == {"receive_failed", "scan_failed"}
                assert interval.progress.issues == result.issues
                assert interval.progress.outcome == "failed"
                assert published[interval][-1] == interval.progress
                assert sum(
                    update.outcome == "failed" for update in published[interval]
                ) == 1
            assert (first.progress.sent, first.progress.replies, first.progress.devices) == (
                1, 2, 2
            )
            assert (second.progress.sent, second.progress.replies, second.progress.devices) == (
                0, 1, 1
            )
            assert (empty.progress.sent, empty.progress.replies, empty.progress.devices) == (
                0, 0, 0
            )
            assert stopped.progress == stopped_progress
            assert stopped.progress.outcome == "stopped"
            assert stopped_result.issues == ("receive_failed",)
            assert [item.device_id for item in stopped_result.records] == [ID]
            assert all(transport.closed for transport in transports)
            with pytest.raises(ValueError):
                await first.async_subscribe("owner", lambda _: None)
            for interval in published:
                await interval.async_stop()
                interval.close()
                assert interval.progress == frozen[interval]
            second_owner()
            stopped_owner()
            assert await stopped.async_stop() == stopped_result
            assert stopped.progress == stopped_progress

            await fresh.async_subscribe("owner", lambda _: None)
            await asyncio.wait_for(shared_rounds.get(), 3)
            fresh_sender, fresh_receiver = endpoints[(HOST, 0)]
            assert fresh.progress.issues == ()
            assert (fresh.progress.rounds, fresh.progress.sent) == (1, 1)
            assert (fresh.progress.replies, fresh.progress.devices) == (0, 0)
            fresh_receiver.datagram_received(
                packet(identity="020000000003", host="192.0.2.12"),
                ("192.0.2.12", 45678),
            )
            assert fresh_sender.submissions == [(QUERY, ("192.0.2.255", 7083))]
            fresh_result = await fresh.async_stop()
            assert [item.device_id for item in fresh_result.records] == ["020000000003"]
            assert fresh_result.issues == ()
            assert fresh.progress.outcome == "stopped"
            for interval, result in zip(published, results, strict=True):
                assert interval.progress == frozen[interval]
                assert await interval.async_wait() == result
            assert stopped.progress == stopped_progress
        finally:
            for transport in transports:
                transport.error = None
            for interval in (first, second, empty, stopped, fresh):
                await interval.async_stop()


async def test_stop_during_udp_startup_never_submits_a_round(
    hass: HomeAssistant,
) -> None:
    from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

    collector = async_get_collector(hass)
    interval = DiscoveryInterval(hass, "owner")
    entered = asyncio.Event()
    release = asyncio.Event()
    sender = RecordingSender()

    async def open_pending() -> None:
        entered.set()
        await release.wait()
        await open_synthetic(collector)
        collector._senders = [(sender, HOST)]

    with patch.object(collector, "_open", open_pending):
        stop = None
        try:
            await interval.async_subscribe("owner", lambda _: None)
            await asyncio.wait_for(entered.wait(), 3)
            stop = asyncio.create_task(interval.async_stop())
            await asyncio.sleep(0)
            assert interval.progress.outcome == "stopped"
            release.set()
            result = await asyncio.wait_for(stop, 3)
            assert result.records == ()
            assert sender.attempts == 0
            assert (interval.progress.rounds, interval.progress.sent) == (0, 0)
        finally:
            release.set()
            if stop is not None:
                await stop
            await interval.async_stop()




async def start_catalog(hass: HomeAssistant, records: tuple[DiscoveryRecord, ...]):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "user_id": "test-user"}
    )
    with patch(
        "custom_components.uplus_reborn.local_discovery.DiscoveryCollector.scan",
        AsyncMock(return_value=ScanResult(records, ())),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local"}
        )
        interval = hass.data[DISCOVERY_INTERVALS][result["flow_id"]]
        await interval.async_subscribe("test-user", lambda _: None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "local_catalog"
    return result


@pytest.mark.parametrize(
    "kind", ["unsupported", "security", "conflict", "configured", "disabled"]
)
async def test_forbidden_add_cannot_probe_or_create(
    hass: HomeAssistant, kind: str
) -> None:
    item = record()
    if kind in ("configured", "disabled"):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=ID,
            data={"device_id": ID, "host": HOST, "port": 56800},
            disabled_by=ConfigEntryDisabler.USER if kind == "disabled" else None,
        )
        entry.add_to_hass(hass)
    if kind == "unsupported":
        item = replace(item, type_id="01" * 32)
    elif kind == "security":
        item = replace(item, security=1)
    elif kind == "conflict":
        item = replace(item, conflict=True)
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await start_catalog(hass, (item,))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": ID, "action": "view"}
        )
        assert result["step_id"] == "local_details"
        assert "local_add" not in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_technical"}
        )
        assert result["step_id"] == "local_technical"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert "local_add" not in result["menu_options"]
        handler = hass.config_entries.flow._progress[result["flow_id"]]
        forged = await handler.async_step_local_add({})
        assert forged["step_id"] == "local_details"
        probe.assert_not_awaited()


async def test_stale_retention_counts_and_manual_guard(hass: HomeAssistant) -> None:
    result = await start_catalog(hass, (record(type_id="01" * 32),))
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    with patch(
        "custom_components.uplus_reborn.local_discovery.DiscoveryCollector.scan",
        AsyncMock(return_value=ScanResult((), ())),
    ):
        await handler.async_step_local()
        await hass.data[DISCOVERY_INTERVALS][result["flow_id"]].async_subscribe(
            "test-user", lambda _: None
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await handler.async_step_local({})
    result = await handler.async_step_local_catalog()
    assert result["description_placeholders"]["count"] == "0"
    assert result["description_placeholders"]["retained"] == "1"
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await handler.async_step_manual(
            {"host": HOST, "port": 56800, "device_id": ID, "confirm_model": True}
        )
        assert result["errors"] == {"base": "incompatible_discovery"}
        probe.assert_not_awaited()


async def test_explicit_model_confirmation_precedes_manual_tcp(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": HOST, "port": 56800, "device_id": ID, "confirm_model": False},
        )
        assert result["errors"] == {"base": "model_confirmation_required"}
        probe.assert_not_awaited()


async def test_supported_failed_tcp_keeps_catalog_and_explicit_navigation(
    hass: HomeAssistant,
) -> None:
    from custom_components.uplus_reborn.protocol import CannotConnect

    result = await start_catalog(hass, (record(),))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": ID, "action": "view"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_add"}
    )
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(side_effect=CannotConnect("offline")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"action": "add"}
        )
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "catalog"}
    )
    assert result["description_placeholders"]["count"] == "1"


async def test_duplicate_creation_race_creates_only_one_entry(
    hass: HomeAssistant,
) -> None:
    first = await start_catalog(hass, (record(),))
    second = await start_catalog(hass, (record(),))
    for result in (first, second):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": ID, "action": "view"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_add"}
        )
    entered = 0
    both_entered = asyncio.Event()

    async def validate(_: dict) -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()

    with (
        patch(
            "custom_components.uplus_reborn.config_flow._async_validate_connection",
            validate,
        ),
        patch(
            "custom_components.uplus_reborn.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        outcomes = await asyncio.gather(
            *(
                hass.config_entries.flow.async_configure(
                    result["flow_id"], {"action": "add"}
                )
                for result in (first, second)
            )
        )
        await hass.async_block_till_done()
    assert (
        sum(result["type"] is FlowResultType.CREATE_ENTRY for result in outcomes) == 1
    )
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_other_integration_mac_association_is_informational(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import device_registry as dr

    other = MockConfigEntry(domain="mqtt", data={})
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(ID))},
    )
    result = await start_catalog(hass, (record(),))
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": ID, "action": "view"}
        )
        assert "local_add" in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_technical"}
        )
        assert result["description_placeholders"]["other"] == "mqtt"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert "local_add" in result["menu_options"]
        probe.assert_not_awaited()


async def test_early_finish_keeps_valid_observations_and_other_scan(
    hass: HomeAssistant,
) -> None:
    """User stop freezes one scan without dropping results or stopping another."""
    collector = DiscoveryCollector(hass)

    async def open_no_sockets() -> None:
        await open_synthetic(collector)

    activity = ScanActivity()
    other_activity = ScanActivity()
    with patch.object(collector, "_open", open_no_sockets):
        first = asyncio.create_task(collector.scan(activity=activity))
        other = asyncio.create_task(collector.scan(activity=other_activity))
        await asyncio.sleep(0)
        collector.receive(packet()[:-1], HOST)
        assert activity.replies == 0
        for _ in range(20):
            collector.receive(packet(), HOST)
        collector.receive(packet(type_id="01" * 32, identity="020000000002"), HOST)
        activity.stop.set()
        collector.receive(packet(identity="020000000003"), HOST)
        result = await first
        snapshot = activity.snapshot()
        assert {item.device_id for item in result.records} == {ID, "020000000002"}
        assert (snapshot.replies, snapshot.devices, snapshot.outcome) == (
            21,
            2,
            "stopped",
        )
        assert len(snapshot.events) <= 12
        assert not other.done()
        assert other_activity.replies == 22 and other_activity.devices == 3
        collector.receive(packet(), HOST)
        assert activity.snapshot() == snapshot
        other_activity.stop.set()
        await other


async def test_native_continue_stops_only_its_scan_without_tcp(
    hass: HomeAssistant,
) -> None:
    """The first reply never replaces the form; explicit submit stops without TCP."""
    collector = async_get_collector(hass)

    async def open_no_sockets() -> None:
        await open_synthetic(collector)

    with (
        patch.object(collector, "_open", open_no_sockets),
        patch(
            "custom_components.uplus_reborn.config_flow._async_validate_connection",
            AsyncMock(),
        ) as probe,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER, "user_id": "test-user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local"}
        )
        interval = hass.data[DISCOVERY_INTERVALS][result["flow_id"]]
        await interval.async_subscribe("test-user", lambda _: None)
        await asyncio.sleep(0)
        collector.receive(packet(type_id="01" * 32), HOST)
        await asyncio.sleep(0.02)
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "local"
        assert result["type"] is FlowResultType.FORM
        collector.receive(packet(type_id="01" * 32), HOST)
        assert interval.progress.replies == 2
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "local_catalog"
        assert result["description_placeholders"]["count"] == "1"
        assert interval.progress.outcome == "stopped"
        probe.assert_not_awaited()
        assert not hass.config_entries.async_entries(DOMAIN)
        frozen = result["description_placeholders"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["description_placeholders"] == frozen
        hass.config_entries.flow.async_abort(result["flow_id"])




async def test_real_preview_websocket_rejects_foreign_flows_and_disconnects(
    hass: HomeAssistant, hass_ws_client, hass_admin_user, loopback_socket
) -> None:
    refresh = await hass.auth.async_create_refresh_token(
        hass_admin_user, client_id="http://localhost/"
    )
    token = hass.auth.async_create_access_token(refresh)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "user_id": "another-user"}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"}
    )
    client = await hass_ws_client(hass, access_token=token)
    command = {
        "id": 1, "type": f"{DOMAIN}/start_preview",
        "flow_type": "config_flow", "flow_id": result["flow_id"], "user_input": {},
    }
    await client.send_json(command)
    rejected = await client.receive_json()
    assert not rejected["success"] and rejected["error"]["code"] == "invalid_flow"
    hass.config_entries.flow.async_abort(result["flow_id"])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "user_id": hass_admin_user.id}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"}
    )
    collector = async_get_collector(hass)
    with patch.object(collector, "_open", lambda: open_synthetic(collector)):
        await client.send_json({**command, "id": 2, "flow_id": result["flow_id"]})
        subscribed = await client.receive_json()
        assert subscribed["success"]
        preview = await client.receive_json()
        assert preview["event"]["attributes"]["entity_picture"].endswith("/discovery.svg")
        interval = hass.data[DISCOVERY_INTERVALS][result["flow_id"]]
        await client.close()
        await interval.async_wait()
        assert interval.progress.outcome == "disconnected"


async def test_new_search_does_not_inherit_previous_coverage_failure(
    hass: HomeAssistant,
) -> None:
    """Immediate results belong to the new interval, not the last failed search."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "user_id": "owner"}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"}
    )
    with patch(
        "custom_components.uplus_reborn.local_discovery.async_get_adapters",
        AsyncMock(return_value=[]),
    ):
        interval = hass.data[DISCOVERY_INTERVALS][result["flow_id"]]
        await interval.async_subscribe("owner", lambda _: None)
        await interval.async_wait()
        await asyncio.sleep(0)
    failed = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert interval.progress.issues == ("no_enabled_interfaces",)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "local"}
    )
    interval = hass.data[DISCOVERY_INTERVALS][result["flow_id"]]
    fresh = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert interval.progress.issues == ()
    assert fresh["description_placeholders"]["issues"] != failed["description_placeholders"]["issues"]




@pytest.mark.parametrize("action, step", [("local", "local"), ("manual", "manual")])
async def test_empty_catalog_routes_do_not_require_a_device(
    hass: HomeAssistant, action: str, step: str,
) -> None:
    result = await start_catalog(hass, ())
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"action": action}
        )
        assert result["step_id"] == step
        probe.assert_not_awaited()


async def test_empty_catalog_view_requires_a_dropdown_choice(hass: HomeAssistant) -> None:
    result = await start_catalog(hass, ())
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"action": "view"}
        )
        assert result["step_id"] == "local_catalog"
        assert result["errors"] == {"base": "catalog_choice_required"}
        probe.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"device_id": "__last_search_activity__", "action": "view"},
        )
        assert result["step_id"] == "local_activity"
        assert result["description_placeholders"]["devices"] == "0"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "local_catalog"
        assert result["description_placeholders"]["count"] == "0"
        assert result["description_placeholders"]["retained"] == "0"
        probe.assert_not_awaited()


async def test_catalog_inspects_the_chosen_identity_and_respects_action(
    hass: HomeAssistant,
) -> None:
    other_id = "020000000002"
    result = await start_catalog(
        hass,
        (record(), record(device_id=other_id, type_id="01" * 32, host="192.0.2.11")),
    )
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": other_id, "action": "view"}
        )
        assert result["step_id"] == "local_details"
        assert result["description_placeholders"]["device_id"] == other_id
        assert "local_add" not in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_catalog"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": other_id, "action": "manual"}
        )
        assert result["step_id"] == "manual"
        probe.assert_not_awaited()


async def test_journal_choice_never_becomes_a_device_or_overrides_manual_action(
    hass: HomeAssistant,
) -> None:
    result = await start_catalog(hass, (record(),))
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"device_id": "__last_search_activity__", "action": "view"},
        )
        assert result["step_id"] == "local_activity"
        frozen = result["description_placeholders"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["description_placeholders"] == frozen
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "local_catalog"
        assert result["description_placeholders"]["count"] == "1"
        assert result["description_placeholders"]["retained"] == "1"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"device_id": "__last_search_activity__", "action": "manual"},
        )
        assert result["step_id"] == "manual"
        probe.assert_not_awaited()


async def test_shared_model_type_is_one_non_addable_device(
    hass: HomeAssistant,
) -> None:
    """A catalog family never becomes a guessed model or extra responders."""
    shared_type = "200861051c408504012700718001990000000000000071800162b00000000040"
    result = await start_catalog(hass, (record(type_id=shared_type),))
    assert result["description_placeholders"]["count"] == "1"
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": ID, "action": "view"}
        )
        facts = result["description_placeholders"]
        assert set(facts["models"].splitlines()) == {
            "- H3F-280WSAAU1", "- H3F-320FSAAU1", "- H3F-320WSAAU1"
        }
        assert "local_add" not in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_technical"}
        )
        assert result["step_id"] == "local_technical"
        assert result["description_placeholders"]["type_id"] == shared_type
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "local_details"
        assert result["description_placeholders"] == facts
        probe.assert_not_awaited()


@pytest.mark.parametrize(
    "type_id,model,category",
    [
        (
            "2010f0c2107387200121458c47df2b000000bb751c529fcd112a7835461dbec0",
            "- HTF-552DGS6U1",
            "Refrigerator",
        ),
        (
            "201c12002400081005010021800067414800bc00000000000000000000000040",
            None,
            "Washing machine",
        ),
        ("01" * 32, None, "Unknown category"),
    ],
)
async def test_catalog_identification_never_infers_a_profile_class_model(
    hass: HomeAssistant, type_id: str, model: str | None, category: str,
) -> None:
    """Exact catalog facts, category-only types and unknowns stay distinct."""
    result = await start_catalog(hass, (record(type_id=type_id),))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": ID, "action": "view"}
    )
    facts = result["description_placeholders"]
    assert facts["category"] == category
    if model is not None:
        assert facts["models"] == model
    else:
        assert "HWD120" not in facts["models"] and "HW120" not in facts["models"]
        assert facts["catalog_source"] == "—"
    assert "local_add" not in result["menu_options"]
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        forged = await handler.async_step_local_add({"action": "add"})
        assert forged["step_id"] == "local_details"
        probe.assert_not_awaited()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_model_family_preserves_distinct_identity_and_selected_device(
    hass: HomeAssistant,
) -> None:
    """Model families never merge devices or lose the chosen row on return."""
    shared_type = "200861051c408504012700718001990000000000000071800162b00000000040"
    other_id = "020000000002"
    result = await start_catalog(
        hass,
        (
            record(type_id=shared_type),
            record(type_id=shared_type, device_id=other_id, host="192.0.2.11"),
        ),
    )
    assert result["description_placeholders"]["count"] == "2"
    choices = result["data_schema"].schema["device_id"].config["options"]
    assert {choice["value"] for choice in choices} == {
        ID, other_id, "__last_search_activity__"
    }
    for identity, host in ((ID, HOST), (other_id, "192.0.2.11")):
        choice = next(choice for choice in choices if choice["value"] == identity)
        assert identity in choice["label"] and host in choice["label"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": other_id, "action": "view"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_technical"}
    )
    assert result["description_placeholders"]["device_id"] == other_id
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["description_placeholders"]["device_id"] == other_id
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_catalog"}
    )
    marker = next(key for key in result["data_schema"].schema if key == "device_id")
    assert marker.default() == other_id


async def test_technical_return_rechecks_newly_configured_entry(
    hass: HomeAssistant,
) -> None:
    """Concurrent addition revokes eligibility without hiding identification."""
    result = await start_catalog(hass, (record(),))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": ID, "action": "view"}
    )
    assert "local_add" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_technical"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ID,
        data={"device_id": ID, "host": HOST, "port": 56800},
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert "local_add" not in result["menu_options"]
    assert result["description_placeholders"]["configured"] == "Yes"
    assert result["description_placeholders"]["models"] == "- HWD120-B1558U"


async def test_missing_technical_selection_returns_to_catalog_without_probe(
    hass: HomeAssistant,
) -> None:
    result = await start_catalog(hass, ())
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    with patch(
        "custom_components.uplus_reborn.config_flow._async_validate_connection",
        AsyncMock(),
    ) as probe:
        for submission in (None, {}):
            result = await handler.async_step_local_technical(submission)
            assert result["step_id"] == "local_catalog"
        probe.assert_not_awaited()


async def test_independent_catalog_statuses_survive_stale_retention(
    hass: HomeAssistant,
) -> None:
    """Configured, type/security, conflict and freshness are not exclusive states."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ID, disabled_by=ConfigEntryDisabler.USER,
        data={"device_id": ID, "host": HOST, "port": 56800},
    )
    entry.add_to_hass(hass)
    result = await start_catalog(
        hass, (record(type_id="01" * 32, security=1, conflict=True),)
    )
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    with patch(
        "custom_components.uplus_reborn.local_discovery.DiscoveryCollector.scan",
        AsyncMock(return_value=ScanResult((), ())),
    ):
        await handler.async_step_local()
        await hass.data[DISCOVERY_INTERVALS][result["flow_id"]].async_subscribe(
            "test-user", lambda _: None
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await handler.async_step_local({})
    result = await handler.async_step_local_details()
    assert result["step_id"] == "local_catalog"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": ID, "action": "view"}
    )
    facts = result["description_placeholders"]
    assert (facts["configured"], facts["supported"], facts["conflict"], facts["latest"]) == (
        "Yes", "No", "Yes", "No"
    )
    assert "local_add" not in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_technical"}
    )
    technical = result["description_placeholders"]
    assert technical["security"] == "1"
    for key in ("configured", "supported", "conflict", "latest"):
        assert technical[key] == facts[key]


@pytest.mark.parametrize("admin", [False, True])
async def test_preview_requires_admin_and_an_active_local_form(
    hass: HomeAssistant, hass_ws_client, loopback_socket, admin: bool,
) -> None:
    """Native authentication and current-step checks both precede acquisition."""
    user = await hass.auth.async_create_user(
        "Preview owner", group_ids=["system-admin"] if admin else ["system-users"]
    )
    refresh = await hass.auth.async_create_refresh_token(user, client_id="http://localhost/")
    token = hass.auth.async_create_access_token(refresh)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "user_id": user.id}
    )
    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "local"}
    )
    interval = hass.data[DISCOVERY_INTERVALS][flow["flow_id"]]
    if admin:
        await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    client = await hass_ws_client(hass, access_token=token)
    with patch(
        "custom_components.uplus_reborn.local_discovery.async_get_adapters",
        AsyncMock(return_value=[]),
    ) as adapters:
        await client.send_json({
            "id": 1, "type": f"{DOMAIN}/start_preview", "flow_type": "config_flow",
            "flow_id": flow["flow_id"], "user_input": {},
        })
        response = await client.receive_json()
        assert not response["success"]
        assert response["error"]["code"] == ("invalid_flow" if admin else "unauthorized")
        assert interval.progress.sent == 0
        adapters.assert_not_called()
    await client.close()
    hass.config_entries.flow.async_abort(flow["flow_id"])


async def test_cancelled_cleanup_releases_every_stopped_observation(
    hass: HomeAssistant,
) -> None:
    """Closing a flow during another interval's teardown cannot retain its observations."""
    class TrackedActivity(ScanActivity):
        """Allow lifetime observation without inspecting collector registrations."""

    collector = DiscoveryCollector(hass)
    activities = (TrackedActivity(), TrackedActivity())
    lifetimes = tuple(weakref.ref(activity) for activity in activities)
    ready, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_sleep = asyncio.sleep

    async def sleep(delay):
        if delay != 20:
            return await actual_sleep(delay)
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            await release.wait()
            raise

    with (
        patch.object(collector, "_open", lambda: open_synthetic(collector)),
        patch("custom_components.uplus_reborn.local_discovery.asyncio.sleep", sleep),
    ):
        tasks = [asyncio.create_task(collector.scan(activity=item)) for item in activities]
        try:
            await asyncio.wait_for(ready.wait(), 3)
            assert activities[0].sent == activities[1].sent == 1
            collector.receive(packet(), HOST)
            activities[0].stop.set()
            activities[1].stop.set()
            await asyncio.wait_for(closing.wait(), 3)
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
        finally:
            release.set()
            activities[0].stop.set()
            activities[1].stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    del activities, tasks
    await actual_sleep(0)
    gc.collect()
    assert all(lifetime() is None for lifetime in lifetimes)
