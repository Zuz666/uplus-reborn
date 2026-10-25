"""Synthetic loopback LAN for real-Core discovery acceptance; never live access."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import asdict
from itertools import pairwise
from typing import Any, Self

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType
from homeassistant.components.network.network import async_get_network
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

DOMAIN = "uplus_reborn"
TYPE = "111c12002400081005010021800088410000d500000000000000000000000000"
FREEZER = "200861051c408504160400718001990000000000000071800162420000000040"
MODEL_TYPES = {
    8: "200861051c408504012700718001990000000000000071800162b00000000040",
    9: "2010f0c2107387200121458c47df2b000000bb751c529fcd112a7835461dbec0",
    10: "200861051c408504012300618002960000000000000060841164410000000040",
    11: "200861051c408504012700718001990000000000000071800162b00000000040",
}


def _record(number: int, type_id: str, flags: int = 0) -> bytes:
    body = bytearray(288)
    body[:16] = f"0200000000{number:02X}".encode().ljust(16, b"\0")
    body[16:48] = bytes.fromhex(type_id)
    body[208:224] = f"127.0.0.{number}".encode().ljust(16, b"\0")
    return b"Haier" + struct.pack(">IH6sI", 0x684D, flags, bytes(6), 288) + body


class _QueryReceiver(asyncio.DatagramProtocol):
    """Identify a second synthetic broadcast destination without changing UDP."""

    def __init__(self, fixture: DiscoveryFixture, destination: str) -> None:
        self.fixture = fixture
        self.destination = destination

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.fixture._receive_query(data, addr, self.destination)


class _DiscoveryTiming:
    """Control only the collector module's waits, never asyncio's global clock."""

    def __init__(self, mode: str) -> None:
        self.fast = mode == "fast"
        self.waiter: asyncio.Future | None = None
        self.ready = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)

    def __enter__(self) -> Self:
        from custom_components.uplus_reborn import local_discovery

        self.module = local_discovery
        self.original = local_discovery.asyncio
        if self.fast:
            local_discovery.asyncio = self
        return self

    def __exit__(self, *exc: object) -> None:
        self.module.asyncio = self.original

    async def sleep(self, delay: float) -> None:
        if delay == 0:
            await self.original.sleep(0)
            return
        assert delay == 20, f"Discovery requested unexpected cadence: {delay}"
        waiter = asyncio.get_running_loop().create_future()
        assert self.waiter is None, "Overlapping discovery send loops"
        self.waiter = waiter
        self.ready.set()
        try:
            await waiter
        finally:
            if self.waiter is waiter:
                self.waiter = None
                self.ready.clear()

    async def advance(self) -> None:
        if not self.fast:
            return
        async with asyncio.timeout(5):
            await self.ready.wait()
            assert self.waiter is not None and not self.waiter.done()
            self.waiter.set_result(None)
            await asyncio.sleep(0)


class DiscoveryFixture(asyncio.DatagramProtocol):
    """Use real production adapter selection, sockets, flow and TCP validation.

    Only the disposable HA network inventory is synthetic. Prefix-derived
    127.0.1.255 is routed inside Linux loopback, not a physical LAN broadcast.
    Physical egress remains a separate owner-authorized acceptance scenario.
    """

    def __init__(self, hass: Any, peer_factory: Any, result: dict[str, Any]) -> None:
        self.hass = hass
        self.peer_factory = peer_factory
        self.result = result
        self.transports: list[asyncio.DatagramTransport] = []
        self.responders: dict[int, asyncio.DatagramTransport] = {}
        self.peers: list[Any] = []
        self.added: list[str] = []
        self.queries: list[dict[str, Any]] = []
        self.silent = False
        self.network = None
        self.original_adapters = None
        self.pending_replies: list[asyncio.TimerHandle] = []
        self.query_received = asyncio.Event()
        self.lifecycle_flows: list[str] = []
        self.shutdown_interval: Any = None
        self.shutdown_unsubscribe: Any = None
        self.shutdown_errors: list[dict[str, Any]] = []
        self.shutdown_log_handler: logging.Handler | None = None
        self.shutdown_late_interval: Any = None
        self.shutdown_late_done = asyncio.Event()
        self.shutdown_late_unsubscribe: Any = None
        self.timing: _DiscoveryTiming | None = None

    async def exercise_acceptance(self, http_port: int, mode: str, reason: str) -> None:
        receipt = {
            "mode": mode, "reason": reason,
            "real_time": {
                "cadence": "not_applicable" if mode == "fast" else "not_run",
                "continuous": "not_applicable" if mode == "fast" else "not_run",
            },
            "phases": {name: {"status": "not_run"} for name in (
                "baseline", "lifecycle", "shared_sender",
            )},
        }
        self.result["discovery_timing"] = receipt
        with _DiscoveryTiming(mode) as timing:
            self.timing = timing
            try:
                for name, exercise in (
                    ("baseline", self.exercise),
                    ("lifecycle", lambda: self.exercise_lifecycle(http_port)),
                    ("shared_sender", self.exercise_shared_sender),
                ):
                    phase = receipt["phases"][name]
                    started = self.hass.loop.time()
                    try:
                        await exercise()
                    except BaseException:
                        phase["status"] = "failed"
                        raise
                    else:
                        group = self.result.get({
                            "lifecycle": "discovery_lifecycle",
                            "shared_sender": "discovery_shared_sender",
                        }.get(name, ""), {})
                        phase["status"] = "failed" if any(
                            isinstance(value, dict) and value.get("status") == "failed"
                            for value in group.values()
                        ) else "passed"
                    finally:
                        phase["elapsed_seconds"] = round(self.hass.loop.time() - started, 3)
            finally:
                self.timing = None

    async def _advance(self) -> None:
        if self.timing is not None:
            await self.timing.advance()

    async def start(self, *, identification: bool = False) -> None:
        self.network = await async_get_network(self.hass)
        self.original_adapters = self.network.adapters
        self.network.adapters = [
            {
                "name": "loopback-discovery-fixture",
                "index": 1,
                "enabled": True,
                "default": True,
                "auto": True,
                "ipv4": [{"address": "127.0.0.1", "network_prefix": 23}],
                "ipv6": [],
            }
        ]
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.1.255", 7083)
        )
        self.transports.append(transport)
        numbers = (1, 2, 3, 4, 5, 7, *MODEL_TYPES) if identification else (1, 2, 3, 4, 5, 7)
        for number in numbers:
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=(f"127.0.0.{number}", 0)
            )
            self.responders[number] = transport
            self.transports.append(transport)
        for number in (2, 6, 7):
            peer = self.peer_factory(f"0200000000{number:02X}")
            await peer.start(f"127.0.0.{number}", 56800)
            self.peers.append(peer)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._receive_query(data, addr, "127.0.1.255")

    def _receive_query(
        self, data: bytes, addr: tuple[str, int], destination: str
    ) -> None:
        if len(data) != 77 or data[:9] != b"Haier\0\0i\x15":
            raise RuntimeError("Unexpected discovery query")
        self.queries.append(
            {
                "at": self.hass.loop.time(), "sender_address": addr[0],
                "sender_port": addr[1], "destination": destination, "bytes": len(data),
            }
        )
        self.query_received.set()
        if self.silent:
            return
        self.pending_replies.append(self.hass.loop.call_later(1, self._respond, addr))

    def _respond(self, addr: tuple[str, int]) -> None:
        """Make the native waiting/progress state observable before the burst."""
        self.pending_replies[:] = [
            handle
            for handle in self.pending_replies
            if handle.when() > self.hass.loop.time()
        ]
        for number, transport in self.responders.items():
            type_id = FREEZER if number == 3 else "01" * 32 if number == 4 else TYPE
            type_id = MODEL_TYPES.get(number, type_id)
            reply = _record(number, type_id, 2 if number == 5 else 0)
            # Independent socket paths, duplicated replies, and malformed noise.
            transport.sendto(reply, addr)
            transport.sendto(reply, (addr[0], 7083))
            transport.sendto(reply, addr)
            transport.sendto(reply[:-1], addr)

    async def _catalog(self, flow_id: str) -> dict[str, Any]:
        result = await self.hass.config_entries.flow.async_configure(flow_id, {})
        assert result["step_id"] == "local_catalog", result
        return result

    async def exercise_identification(self) -> None:
        """Exercise changed views over real UDP without replaying the wire campaign."""
        original_entries = {
            entry.entry_id: dict(entry.data)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER, "user_id": "fixture-user"}
        )
        flow_id = result["flow_id"]
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "local"}
        )
        interval = self.hass.data[f"{DOMAIN}_discovery_intervals"][flow_id]
        await interval.async_subscribe("fixture-user", lambda _: None)
        await asyncio.sleep(2)
        result = await self._catalog(flow_id)
        assert result["description_placeholders"]["count"] == "10"
        frozen_queries = len(self.queries)
        views = {}
        for number in (1, 2, 3, 4, 5, 8, 9, 10, 11):
            identity = f"0200000000{number:02X}"
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"device_id": identity, "action": "view"}
            )
            card = result["description_placeholders"]
            assert card["device_id"] == identity
            assert ("local_add" in result["menu_options"]) == (number == 2)
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"next_step_id": "local_technical"}
            )
            assert result["step_id"] == "local_technical"
            technical = result["description_placeholders"]
            assert technical["device_id"] == identity
            assert technical["models"] == card["models"]
            result = await self.hass.config_entries.flow.async_configure(flow_id, {})
            assert result["step_id"] == "local_details"
            assert result["description_placeholders"] == card
            views[identity] = {"card": card, "technical": technical}
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"next_step_id": "local_catalog"}
            )
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"device_id": "__last_search_activity__", "action": "view"}
        )
        assert result["step_id"] == "local_activity"
        journal = result["description_placeholders"]
        assert journal["devices"] == "10"
        await self.hass.config_entries.flow.async_configure(flow_id, {})
        self.hass.config_entries.flow.async_abort(flow_id)
        assert not any(peer.queries for peer in self.peers)
        assert len(self.queries) == frozen_queries
        assert {
            entry.entry_id: dict(entry.data)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        } == original_entries
        self.result["discovery_identification"] = {
            "synthetic_responders": True,
            "views": views,
            "journal": journal,
            "queries_during_browsing": 0,
            "tcp_during_browsing": 0,
            "entries_unchanged": True,
            "physical_hardware_verified": False,
        }

    async def exercise(self) -> None:
        original_entries = {
            entry.entry_id: dict(entry.data)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER, "user_id": "fixture-user"}
        )
        flow_id = result["flow_id"]
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "local"}
        )
        assert result["type"] is FlowResultType.FORM
        interval = self.hass.data[f"{DOMAIN}_discovery_intervals"][flow_id]
        await interval.async_subscribe("fixture-user", lambda _: None)
        # Full mode crosses the removed deadline; fast mode proves ordered rounds.
        if self.timing is not None and self.timing.fast:
            for round_number in range(1, 8):
                await self._queries_at_least(round_number)
                await self._wait_replies(interval, round_number * 18)
                if round_number < 7:
                    await self._advance()
        else:
            self.result["discovery_timing"]["real_time"]["continuous"] = "failed"
            await asyncio.sleep(122)
        assert interval.progress.outcome == "searching"
        assert len(self.queries) == 7
        if self.timing is not None and not self.timing.fast:
            self.result["discovery_timing"]["real_time"]["continuous"] = "passed"
        result = await self._catalog(flow_id)
        assert result["description_placeholders"]["count"] == "6"
        for number in (1, 3, 4, 5):
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"device_id": f"0200000000{number:02X}", "action": "view"}
            )
            assert "local_add" not in result["menu_options"]
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"next_step_id": "local_catalog"}
            )
        assert not any(peer.queries for peer in self.peers)
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"action": "local"}
        )
        assert result["type"] is FlowResultType.FORM
        interval = self.hass.data[f"{DOMAIN}_discovery_intervals"][flow_id]
        await interval.async_subscribe("fixture-user", lambda _: None)
        await asyncio.sleep(2)
        state = interval.progress
        assert state.devices == 6 and state.replies == 18
        assert not any(peer.queries for peer in self.peers)
        result = await self._catalog(flow_id)
        assert result["description_placeholders"]["count"] == "6"
        assert result["description_placeholders"]["rounds"] == "1"
        assert int(result["description_placeholders"]["elapsed"]) < 10
        assert len(self.queries) == 8
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"device_id": "020000000002", "action": "view"}
        )
        assert "local_add" in result["menu_options"]
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "local_add"}
        )
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"action": "add"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        self.added.append(result["result"].entry_id)
        await self.hass.async_block_till_done()
        assert self.peers[0].queries
        # A second real TCP-only identity exercises the explicit manual fallback.
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "manual"}
        )
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "127.0.0.6",
                "port": 56800,
                "device_id": "020000000006",
                "confirm_model": True,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        self.added.append(result["result"].entry_id)
        await self.hass.async_block_till_done()
        assert self.peers[1].queries
        assert all(
            dict(self.hass.config_entries.async_get_entry(key).data) == data
            for key, data in original_entries.items()
        )
        assert not any(peer.rejected for peer in self.peers)
        self.result["discovery"] = {
            "queries": list(self.queries),
            "distinct_responders": 6,
            "continuous_seconds": None if self.timing and self.timing.fast else 122,
            "query_cadence_seconds": 20,
            "ephemeral_and_fixed_replies": True,
            "early_finish_seconds_below": 10,
            "live_distinct_devices": 6,
            "live_valid_replies": 18,
            "early_finish_no_tcp": True,
            "unsupported_and_configured_no_tcp": True,
            "local_entry_created": True,
            "manual_entry_created": True,
            "existing_entries_unchanged": True,
            "physical_broadcast_egress": False,
        }

    async def _queries_at_least(self, count: int) -> None:
        """Wait for observed datagrams, not a guessed scheduler delay."""
        async with asyncio.timeout(25):
            while len(self.queries) < count:
                self.query_received.clear()
                await self.query_received.wait()

    async def _new_interval(self, user_id: str) -> tuple[str, Any]:
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER, "user_id": user_id}
        )
        flow_id = result["flow_id"]
        self.lifecycle_flows.append(flow_id)
        result = await self.hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "local"}
        )
        assert result["step_id"] == "local"
        return flow_id, self.hass.data[f"{DOMAIN}_discovery_intervals"][flow_id]

    async def _wait_rounds(self, interval: Any, rounds: int) -> None:
        async with asyncio.timeout(25):
            while interval.progress.rounds < rounds:
                assert interval.progress.outcome == "searching", interval.progress
                await asyncio.sleep(0.05)

    async def _wait_replies(self, interval: Any, replies: int) -> None:
        async with asyncio.timeout(5):
            while interval.progress.replies < replies:
                assert interval.progress.outcome == "searching", interval.progress
                await asyncio.sleep(0.05)

    @staticmethod
    def _reserve_released(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Exclusive binding proves release of fixed and ephemeral source ports."""
        addresses = {(query["sender_address"], 7083) for query in queries}
        addresses.update(
            (query["sender_address"], query["sender_port"]) for query in queries
        )
        reservations = []
        try:
            for address in sorted(addresses):
                reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                reservations.append(reservation)
                reservation.bind(address)
            return [{"address": address, "port": port} for address, port in sorted(addresses)]
        finally:
            for reservation in reservations:
                reservation.close()

    def _shared_receipt(self) -> dict[str, Any]:
        """Retain teardown evidence even when preparation failed before the shared phase."""
        if "discovery_shared_sender" not in self.result:
            self.result["discovery_shared_sender"] = {
                "scope": "isolated real Core; two synthetic loopback sender/broadcast pairs",
                "physical_hardware_verified": False,
                "query_start_index": len(self.queries),
                "shared_rounds": {"status": "not_run"},
                "recoverable_and_fatal": {"status": "not_run"},
                "shutdown": {"status": "pending"},
                "cleanup": {"fault_hooks_restored": True, "network_inventory_restored": True},
                "limits": [
                    "Synthetic loopback only; no physical broadcast, appliance or browser proof.",
                    "Send and receive errors are controlled runtime seams, not kernel fault injection.",
                    "Earlier issue #62 evidence is separate; only this run's receipt proves re-execution.",
                ],
            }
        return self.result["discovery_shared_sender"]

    async def exercise_shared_sender(self) -> None:
        """Exercise issue #63 using real sockets and unmodified 20-second sleeps."""
        receipt = self._shared_receipt()
        receipt["query_start_index"] = len(self.queries)
        receipt["cleanup"]["network_inventory_restored"] = False
        original_adapters = self.network.adapters
        second = dict(original_adapters[0])
        second.update(
            name="loopback-discovery-fixture-second", index=2, default=False,
            ipv4=[{"address": "127.0.2.1", "network_prefix": 23}],
        )
        self.network.adapters = [*original_adapters, second]
        transport = None
        try:
            transport, _ = await self.hass.loop.create_datagram_endpoint(
                lambda: _QueryReceiver(self, "127.0.3.255"),
                local_addr=("127.0.3.255", 7083),
            )
            self.transports.append(transport)
            for name, exercise in (
                ("shared_rounds", self._shared_rounds),
                ("recoverable_and_fatal", self._shared_faults),
            ):
                finding = receipt[name]
                try:
                    async with asyncio.timeout(90):
                        await exercise(finding)
                except Exception as err:  # noqa: BLE001
                    finding.update(status="failed", error=f"{type(err).__name__}: {err}")
                else:
                    finding["status"] = "passed"
                finally:
                    active = {
                        flow["flow_id"] for flow in
                        self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                    }
                    for flow_id in self.lifecycle_flows:
                        if flow_id in active:
                            self.hass.config_entries.flow.async_abort(flow_id)
                    self.lifecycle_flows.clear()
                    await self.hass.async_block_till_done()
        finally:
            self.network.adapters = original_adapters
            receipt["cleanup"]["network_inventory_restored"] = (
                self.network.adapters is original_adapters
            )
            if transport is not None:
                transport.close()
            receipt["query_observations"] = self.queries[receipt["query_start_index"]:]

    async def _shared_rounds(self, receipt: dict[str, Any]) -> None:
        start = len(self.queries)
        intervals, unsubscribes = [], []
        frozen = []
        if self.timing is not None and not self.timing.fast:
            self.result["discovery_timing"]["real_time"]["cadence"] = "failed"
        try:
            _, first = await self._new_interval("fixture-shared")
            intervals.append(first)
            unsubscribes.append(await first.async_subscribe("fixture-shared", lambda _: None))
            await self._queries_at_least(start + 2)
            await self._wait_replies(first, 36)
            assert first.progress.rounds == 1 and first.progress.sent == 2
            for _ in range(2):
                await asyncio.sleep(3)
                before = len(self.queries)
                pending = self.timing.waiter if self.timing and self.timing.fast else None
                if self.timing and self.timing.fast:
                    assert pending is not None and not pending.done()
                _, joined = await self._new_interval("fixture-shared")
                intervals.append(joined)
                unsubscribes.append(
                    await joined.async_subscribe("fixture-shared", lambda _: None)
                )
                await self.hass.async_block_till_done()
                assert joined.progress.rounds == joined.progress.sent == 0
                assert joined.progress.replies == joined.progress.devices == 0
                await asyncio.sleep(1)
                assert len(self.queries) == before, "Joining emitted an extra query"
                if self.timing and self.timing.fast:
                    assert self.timing.waiter is pending and not pending.done(), "Join reset the pending round"
                    receipt["controlled_pending_round_preserved"] = True
            receipt["join_observations"] = [asdict(item.progress) for item in intervals]
            # The first subsequent round has three owners, then two, then one.
            for active, expected_round in ((3, 2), (2, 3), (1, 4)):
                await self._advance()
                await self._queries_at_least(start + expected_round * 2)
                await self._wait_rounds(first, expected_round)
                await self._wait_replies(first, expected_round * 36)
                assert len(self.queries) == start + expected_round * 2
                receipt.setdefault("rounds", []).append({
                    "active_intervals": active,
                    "packets": self.queries[start + (expected_round - 1) * 2:],
                    "observations": [asdict(item.progress) for item in intervals],
                })
                for previous, state, result in frozen:
                    assert previous.progress == state
                    assert await previous.async_wait() == result
                stopping = intervals[active - 1]
                state_result = await stopping.async_stop()
                frozen.append((stopping, stopping.progress, state_result))
            round_packets = self.queries[start:]
            for destination in ("127.0.1.255", "127.0.3.255"):
                times = [
                    packet["at"] for packet in round_packets
                    if packet["destination"] == destination
                ]
                assert len(times) == 4
                gaps = [later - earlier for earlier, later in pairwise(times)]
                if not (self.timing and self.timing.fast):
                    assert all(19.5 <= gap <= 24 for gap in gaps), gaps
                receipt.setdefault("cadence", []).append({
                    "destination": destination, "timestamps": times, "gaps_seconds": gaps,
                })
            if self.timing is not None and not self.timing.fast:
                self.result["discovery_timing"]["real_time"]["cadence"] = "passed"
            assert [(item.rounds, item.sent) for _, item, _ in frozen] == [
                (1, 2), (2, 4), (4, 8),
            ]
            receipt["frozen_results"] = [
                {"progress": asdict(state), "result": asdict(result)}
                for _, state, result in frozen
            ]
            receipt["released_ports"] = self._reserve_released(round_packets)
            # Simultaneous startup also shares a fresh immediate round.
            new_intervals = await asyncio.gather(
                *(self._new_interval("fixture-shared") for _ in range(3))
            )
            simultaneous = [item for _, item in new_intervals]
            intervals.extend(simultaneous)
            fresh = simultaneous[0]
            before = len(self.queries)
            reopened = self.hass.loop.time()
            unsubscribes.extend(await asyncio.gather(*(
                item.async_subscribe("fixture-shared", lambda _: None)
                for item in simultaneous
            )))
            await self._queries_at_least(before + 2)
            assert self.queries[before]["at"] - reopened < 2
            assert fresh.progress.rounds == 1 and fresh.progress.sent == 2
            assert all(item.progress.devices == item.progress.replies == 0 for item in simultaneous)
            await asyncio.gather(*(self._wait_replies(item, 36) for item in simultaneous))
            assert len(self.queries) == before + 2
            assert all(item.progress.rounds <= 1 and item.progress.sent <= 2 for item in simultaneous)
            await asyncio.gather(*(item.async_stop() for item in simultaneous))
            receipt["simultaneous_start"] = {
                "intervals": [asdict(item.progress) for item in simultaneous],
                "packet_count": len(self.queries) - before,
                "queries": self.queries[before:],
            }
            for previous, state, result in frozen:
                assert previous.progress == state
                assert await previous.async_wait() == result
            receipt.update(
                fresh_run=asdict(fresh.progress), previous_results_frozen=True,
                new_run_immediate=True, last_owner_release=True,
                packet_count=len(round_packets), pairs_per_round=2,
            )
        finally:
            for unsubscribe in unsubscribes:
                unsubscribe()
            for interval in intervals:
                await interval.async_stop()

    async def _shared_faults(self, receipt: dict[str, Any]) -> None:
        from custom_components.uplus_reborn.local_discovery import async_get_collector

        collector = async_get_collector(self.hass)
        intervals, unsubscribes = [], []
        senders = None
        reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reservation.bind(("127.0.2.1", 7083))
        start = len(self.queries)
        mode = {"error": "recoverable", "attempts": 0}

        class FaultSender:
            def sendto(self, data: bytes, address: tuple[str, int]) -> None:
                mode["attempts"] += 1
                if mode["error"] == "recoverable":
                    raise OSError("Synthetic loopback send failure")
                raise RuntimeError("Synthetic loopback sender termination")

        cleanup = self.result["discovery_shared_sender"]["cleanup"]
        original_silent = self.silent
        try:
            _, first = await self._new_interval("fixture-faults")
            intervals.append(first)
            unsubscribes.append(await first.async_subscribe("fixture-faults", lambda _: None))
            await self._queries_at_least(start + 2)
            await self._wait_replies(first, 30)
            assert "fixed_port_unavailable" in first.progress.issues
            reservation.close()
            senders = list(collector._senders)
            assert len(senders) == 2
            # Exercise the production receive-error callback on a real UDP transport.
            senders[0][0].get_protocol().error_received(OSError("Synthetic receive error"))
            assert "receive_failed" in first.progress.issues
            _, stopped = await self._new_interval("fixture-faults")
            intervals.append(stopped)
            unsubscribes.append(
                await stopped.async_subscribe("fixture-faults", lambda _: None)
            )
            _, late = await self._new_interval("fixture-faults")
            intervals.append(late)
            unsubscribes.append(await late.async_subscribe("fixture-faults", lambda _: None))
            await self.hass.async_block_till_done()
            assert late.progress.rounds == late.progress.sent == late.progress.replies == 0
            assert late.progress.devices == 0
            self.responders[1].sendto(_record(1, TYPE), ("127.0.0.1", 7083))
            await self._wait_replies(late, 1)
            assert late.progress.devices == 1
            stopped_result = await stopped.async_stop()
            stopped_state = stopped.progress
            cleanup["fault_hooks_restored"] = False
            collector._senders[:] = [(FaultSender(), target) for _, target in senders]
            await self._advance()
            await self._wait_rounds(first, 2)
            # Both attempted pairs failed, yet neither active owner terminated.
            assert mode["attempts"] == 2
            assert len(self.queries) == start + 2
            assert first.progress.sent == 2 and late.progress.sent == 0
            assert first.progress.outcome == late.progress.outcome == "searching"
            assert "send_failed" in first.progress.issues
            assert "send_failed" in late.progress.issues
            receipt["zero_success_round"] = {
                "attempted_pairs": mode["attempts"], "packet_count": 0,
                "observations": [asdict(first.progress), asdict(late.progress)],
            }
            # Keep the late owner's one-record observation window distinct, while
            # proving the restored real transports still submit both queries.
            self.silent = True
            collector._senders[:] = senders
            await self._advance()
            await self._queries_at_least(start + 4)
            await self._wait_rounds(first, 3)
            self.silent = original_silent
            assert first.progress.outcome == late.progress.outcome == "searching"
            receipt["recovery_round"] = {
                "queries": self.queries[start + 2:],
                "responders_silenced_to_preserve_distinct_partial_results": True,
                "observations": [asdict(first.progress), asdict(late.progress)],
            }
            mode["error"] = "fatal"
            collector._senders[:] = [(FaultSender(), target) for _, target in senders]
            await self._advance()
            async with asyncio.timeout(25):
                first_result, late_result = await asyncio.gather(
                    first.async_wait(), late.async_wait()
                )
            assert first.progress.outcome == late.progress.outcome == "failed"
            assert len(first_result.records) == 6 and len(late_result.records) == 1
            assert "scan_failed" in first_result.issues and "scan_failed" in late_result.issues
            assert stopped.progress == stopped_state
            assert await stopped.async_wait() == stopped_result
            receipt.update(
                fatal_results=[
                    {"progress": asdict(item.progress), "result": asdict(result)}
                    for item, result in ((first, first_result), (late, late_result))
                ],
                stopped_before_fatal={
                    "progress": asdict(stopped_state), "result": asdict(stopped_result),
                },
                stopped_result_unchanged=True,
                released_ports=self._reserve_released(self.queries[start:]),
            )
            # Fatal run cleanup must not poison the next explicit search.
            _, fresh = await self._new_interval("fixture-faults")
            intervals.append(fresh)
            before = len(self.queries)
            unsubscribes.append(await fresh.async_subscribe("fixture-faults", lambda _: None))
            await self._queries_at_least(before + 2)
            assert not fresh.progress.issues
            await self._wait_replies(fresh, 36)
            await fresh.async_stop()
            receipt["fresh_after_failure"] = asdict(fresh.progress)
        finally:
            reservation.close()
            self.silent = original_silent
            # Never reinstall old transports into an already closed/new collector run.
            if senders is not None and collector._senders and any(
                isinstance(sender, FaultSender) for sender, _ in collector._senders
            ):
                collector._senders[:] = senders
            cleanup["fault_hooks_restored"] = True
            for unsubscribe in unsubscribes:
                unsubscribe()
            for interval in intervals:
                await interval.async_stop()

    @staticmethod
    async def _message(ws: ClientWebSocketResponse) -> dict[str, Any]:
        async with asyncio.timeout(5):
            message = await ws.receive()
        assert message.type is WSMsgType.TEXT, message.type
        return message.json()

    async def _connect(
        self, session: ClientSession, http_port: int, access_token: str
    ) -> ClientWebSocketResponse:
        ws = await session.ws_connect(f"http://127.0.0.1:{http_port}/api/websocket")
        assert (await self._message(ws))["type"] == "auth_required"
        await ws.send_json({"type": "auth", "access_token": access_token})
        assert (await self._message(ws))["type"] == "auth_ok"
        return ws

    @staticmethod
    async def _start_preview(ws: ClientWebSocketResponse, flow_id: str) -> None:
        await ws.send_json({
            "id": 1, "type": f"{DOMAIN}/start_preview", "flow_id": flow_id,
            "flow_type": "config_flow", "user_input": {},
        })

    async def _preview_event(
        self, ws: ClientWebSocketResponse, *, terminal: bool
    ) -> dict[str, Any]:
        async with asyncio.timeout(6):
            while True:
                message = await self._message(ws)
                if message["type"] == "result":
                    assert message["success"], message
                    continue
                assert message["type"] == "event" and message["id"] == 1, message
                payload = message["event"]
                searching = "entity_picture" in payload["attributes"]
                if searching is not terminal:
                    return payload

    async def exercise_lifecycle(self, http_port: int) -> None:
        """Run issue #62 through real authenticated Core WebSocket connections."""
        receipt: dict[str, Any] = {
            "scope": "isolated real Core; authenticated WebSocket and loopback UDP",
            "physical_hardware_verified": False,
            "query_start_index": len(self.queries),
            "preview_disconnect": {"status": "not_run"},
            "stop_publication": {"status": "not_run"},
            "shutdown": {"status": "not_run"},
            "cleanup": {
                "websockets_closed": False, "token_revoked": False,
                "user_removed": False, "fault_hooks_restored": True,
            },
        }
        self.result["discovery_lifecycle"] = receipt
        user = refresh = None
        sockets: list[ClientWebSocketResponse] = []
        try:
            user = await self.hass.auth.async_create_user(
                "Discovery lifecycle fixture", group_ids=["system-admin"],
                local_only=True,
            )
            refresh = await self.hass.auth.async_create_refresh_token(
                user, client_id=f"http://127.0.0.1:{http_port}/"
            )
            access_token = self.hass.auth.async_create_access_token(refresh)
            async with ClientSession() as session:
                for name, exercise in (
                    ("preview_disconnect", self._preview_disconnect),
                    ("stop_publication", self._stop_publication),
                ):
                    finding = receipt[name]
                    try:
                        async with asyncio.timeout(40):
                            await exercise(
                                session, http_port, access_token, user.id, sockets,
                                finding,
                            )
                    except Exception as err:  # noqa: BLE001
                        finding.update(status="failed", error=f"{type(err).__name__}: {err}")
                    else:
                        finding["status"] = "passed"
                    finally:
                        for ws in sockets:
                            await ws.close()
                        active_flows = {
                            flow["flow_id"] for flow in
                            self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                        }
                        for flow_id in self.lifecycle_flows:
                            if flow_id in active_flows:
                                self.hass.config_entries.flow.async_abort(flow_id)
                        self.lifecycle_flows.clear()
                        await self.hass.async_block_till_done()
        finally:
            for ws in sockets:
                await ws.close()
            receipt["cleanup"]["websockets_closed"] = all(ws.closed for ws in sockets)
            if refresh is not None:
                self.hass.auth.async_remove_refresh_token(refresh)
                receipt["cleanup"]["token_revoked"] = True
            if user is not None:
                await self.hass.auth.async_remove_user(user)
                receipt["cleanup"]["user_removed"] = True

    async def _preview_disconnect(
        self, session: ClientSession, http_port: int, access_token: str,
        user_id: str, sockets: list[ClientWebSocketResponse], receipt: dict[str, Any],
    ) -> None:
        from homeassistant.components import websocket_api

        from custom_components.uplus_reborn import discovery_preview

        _, unrelated = await self._new_interval(user_id)
        observations: list[Any] = []
        unsubscribe = await unrelated.async_subscribe(user_id, observations.append)
        before = len(self.queries)
        await self._queries_at_least(before + 1)
        flow_id, pending = await self._new_interval(user_id)
        entered, release, closed, settled = (asyncio.Event() for _ in range(4))
        original_translations = discovery_preview.async_get_translations
        original_close = websocket_api.ActiveConnection.async_handle_close

        async def translations(*args: Any, **kwargs: Any) -> Any:
            task = asyncio.current_task()
            assert task is not None
            task.add_done_callback(lambda _: settled.set())
            entered.set()
            await release.wait()
            return await original_translations(*args, **kwargs)

        def connection_close(connection: Any) -> None:
            original_close(connection)
            if connection.user is not None and connection.user.id == user_id:
                closed.set()

        discovery_preview.async_get_translations = translations
        websocket_api.ActiveConnection.async_handle_close = connection_close
        self.result["discovery_lifecycle"]["cleanup"]["fault_hooks_restored"] = False
        try:
            ws = await self._connect(session, http_port, access_token)
            sockets.append(ws)
            await self._start_preview(ws, flow_id)
            await entered.wait()
            assert pending.progress.sent == 0
            await ws.close()
            # This is Core's actual cleanup completion, not just a client close frame.
            await closed.wait()
            disconnected_queries = len(self.queries)
            release.set()
            await settled.wait()
            await self.hass.async_block_till_done()
            receipt.update(
                translations_gated=True, core_connection_cleanup_observed=True,
                initialization_settled=True, closed_attempt_sent=pending.progress.sent,
                immediate_post_disconnect_queries=len(self.queries) - disconnected_queries,
            )
            assert pending.progress.sent == 0, "Closed initialization sent discovery"
            assert len(self.queries) == disconnected_queries, "Late discovery query"
            try:
                async with asyncio.timeout(2):
                    await pending.async_wait()
            except ValueError:
                receipt["closed_attempt_participation"] = "never_started"
            else:
                assert pending.progress.outcome != "searching", "Orphan search survived"
                receipt["closed_attempt_participation"] = pending.progress.outcome
            # Cross the unchanged production cadence while the unrelated owner lives.
            await self._advance()
            await self._queries_at_least(before + 2)
            await self.hass.async_block_till_done()
            assert len(self.queries) == before + 2
            assert unrelated.progress.outcome == "searching"
            assert unrelated.progress.sent == 2 and pending.progress.sent == 0
            assert observations and observations[-1].outcome == "searching"
            receipt.update(
                unrelated_outcome=unrelated.progress.outcome,
                unrelated_sent=unrelated.progress.sent,
                unrelated_observations=len(observations),
                post_disconnect_queries_from_closed_attempt=0,
                requested_cadence_seconds=20,
            )
        finally:
            release.set()
            discovery_preview.async_get_translations = original_translations
            websocket_api.ActiveConnection.async_handle_close = original_close
            self.result["discovery_lifecycle"]["cleanup"]["fault_hooks_restored"] = True
            unsubscribe()
            await unrelated.async_wait()

    async def _stop_publication(
        self, session: ClientSession, http_port: int, access_token: str,
        user_id: str, sockets: list[ClientWebSocketResponse], receipt: dict[str, Any],
    ) -> None:
        flow_id, interval = await self._new_interval(user_id)
        consumers = []
        for _ in range(2):
            ws = await self._connect(session, http_port, access_token)
            consumers.append(ws)
            sockets.append(ws)
        before = len(self.queries)
        for ws in consumers:
            await self._start_preview(ws, flow_id)
        searching = [await self._preview_event(ws, terminal=False) for ws in consumers]
        await self._queries_at_least(before + 1)
        result = await interval.async_stop()
        frozen = interval.progress
        receipt.update(
            interval_outcome=frozen.outcome, connected_consumers=2,
            searching_payloads=searching, frozen_observation=asdict(frozen),
        )
        deliveries = await asyncio.gather(
            *(self._preview_event(ws, terminal=True) for ws in consumers),
            return_exceptions=True,
        )
        terminal = [payload for payload in deliveries if isinstance(payload, dict)]
        receipt["terminal_payloads"] = terminal
        receipt["terminal_delivery_errors"] = [
            {"consumer": index, "error": type(payload).__name__}
            for index, payload in enumerate(deliveries, start=1)
            if isinstance(payload, BaseException)
        ]
        assert not receipt["terminal_delivery_errors"], "Missing terminal preview payload"
        assert frozen.outcome == "stopped"
        assert all("entity_picture" in payload["attributes"] for payload in searching)
        assert all(
            payload["attributes"].get("icon") == "mdi:lan-disconnect"
            and "entity_picture" not in payload["attributes"]
            for payload in terminal
        )
        await interval.async_stop()
        interval.close()
        interval.close()
        # Both native unsubscribe commands must remain harmless after Stop/Close.
        for ws in consumers:
            await ws.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
            async with asyncio.timeout(5):
                while True:
                    message = await self._message(ws)
                    assert message["type"] != "event", "Duplicate terminal notification"
                    if message.get("id") == 2:
                        assert message["success"], message
                        break
        _, fresh = await self._new_interval(user_id)
        changed = asyncio.Event()

        def observe(state: Any) -> None:
            if state.replies:
                changed.set()

        unsubscribe = await fresh.async_subscribe(user_id, observe)
        try:
            next_query = len(self.queries) + 1
            await self._queries_at_least(next_query)
            # New real UDP replies must not mutate the old terminal observations.
            await changed.wait()
            interval.close()
            await interval.async_stop()
            assert fresh.progress.outcome == "searching" and fresh.progress.replies > 0
            assert interval.progress == frozen
            assert await interval.async_wait() == result
            receipt.update(
                connected_consumers=2, searching_payloads=searching,
                terminal_notifications_per_consumer=1,
                frozen_observation=asdict(frozen), frozen_after_later_udp=True,
                repeated_stop_close_unsubscribe_harmless=True,
                new_interval_outcome=fresh.progress.outcome,
                new_interval_replies=fresh.progress.replies,
            )
        finally:
            unsubscribe()
            await fresh.async_wait()

    async def prepare_shutdown(self) -> None:
        """Leave a live owner for the runner's single, real final Core shutdown."""
        receipt = self.result["discovery_lifecycle"]["shutdown"]
        receipt["status"] = "pending"
        try:
            async with asyncio.timeout(15):
                # Retain the original pre-stop entry disposal without closing the
                # discovery sockets/interval that Core itself must now stop.
                for entry_id in self.added:
                    await self.hass.config_entries.async_unload(entry_id)
                self.added.clear()
                _, self.shutdown_interval = await self._new_interval("fixture-shutdown")
                before = len(self.queries)
                self.shutdown_unsubscribe = await self.shutdown_interval.async_subscribe(
                    "fixture-shutdown", lambda _: None
                )
                await self._queries_at_least(before + 1)
            receipt.update(
                active_before_shutdown=self.shutdown_interval.progress.outcome == "searching",
                queries_before_shutdown=len(self.queries),
                sender_port=self.queries[-1]["sender_port"],
            )
        except Exception as err:  # noqa: BLE001
            receipt.update(status="failed", error=f"{type(err).__name__}: {err}")

        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

        shared_shutdown = self._shared_receipt()["shutdown"]

        async def late_owner(_event: Any) -> None:
            shared_shutdown["queries_at_stop_event"] = len(self.queries)
            shared_shutdown["is_stopping_at_attempt"] = self.hass.is_stopping
            self.shutdown_late_interval = DiscoveryInterval(self.hass, "fixture-shutdown-late")
            try:
                self.shutdown_late_unsubscribe = (
                    await self.shutdown_late_interval.async_subscribe(
                        "fixture-shutdown-late", lambda _: None
                    )
                )
            except ValueError:
                shared_shutdown["late_subscription"] = "rejected"
            except Exception as err:  # noqa: BLE001
                shared_shutdown.update(status="failed", error=type(err).__name__)
            else:
                shared_shutdown["late_subscription"] = "accepted_without_sender"
            finally:
                self.shutdown_late_done.set()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, late_owner)

        errors = self.shutdown_errors

        class ShutdownErrors(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                message = record.getMessage().lower()
                listener_error = "listener" in message and (
                    "unknown" in message or "remov" in message
                )
                if record.levelno >= logging.ERROR or listener_error:
                    errors.append({
                        "logger": record.name, "level": record.levelname,
                        "exception_type": (
                            record.exc_info[0].__name__
                            if record.exc_info and record.exc_info[0] else None
                        ),
                        "listener_removal_error": listener_error,
                    })

        self.shutdown_log_handler = ShutdownErrors(logging.WARNING)
        logging.getLogger().addHandler(self.shutdown_log_handler)

    async def finish_shutdown(self, *, core_stopped: bool) -> None:
        """Measure after hass.async_stop, before any fixture-forced flow cleanup."""
        from custom_components.uplus_reborn.discovery_interval import DiscoveryInterval

        receipt = self.result["discovery_lifecycle"]["shutdown"]
        try:
            receipt["actual_core_stopped"] = core_stopped
            assert core_stopped, "Core failed to stop"
            assert self.shutdown_interval is not None, "No active shutdown interval"
            async with asyncio.timeout(5):
                await self.shutdown_interval.async_wait()
            receipt["outcome"] = self.shutdown_interval.progress.outcome
            receipt["collection_settled"] = True
            assert receipt["outcome"] == "shutdown"
            shared_shutdown = self._shared_receipt()["shutdown"]
            async with asyncio.timeout(5):
                await self.shutdown_late_done.wait()
                assert self.shutdown_late_interval is not None
                if shared_shutdown.get("late_subscription") != "rejected":
                    await self.shutdown_late_interval.async_wait()
            shared_shutdown["late_progress"] = asdict(self.shutdown_late_interval.progress)
            assert shared_shutdown["is_stopping_at_attempt"]
            assert self.shutdown_late_interval.progress.sent == 0
            if shared_shutdown.get("late_subscription") != "rejected":
                assert self.shutdown_late_interval.progress.outcome == "shutdown"
            else:
                assert self.shutdown_late_interval.progress.rounds == 0
            post_stop = DiscoveryInterval(self.hass, "fixture-after-shutdown")
            try:
                unsubscribe = await post_stop.async_subscribe(
                    "fixture-after-shutdown", lambda _: None
                )
            except ValueError:
                shared_shutdown["after_stopped_subscription"] = "rejected"
            else:
                unsubscribe()
                await post_stop.async_stop()
                raise AssertionError("Stopped Core admitted a new discovery owner")
            shared_shutdown["after_stopped_progress"] = asdict(post_stop.progress)
            assert post_stop.progress.rounds == post_stop.progress.sent == 0
            post_stop.close()
            assert len(self.queries) == shared_shutdown["queries_at_stop_event"]
            assert shared_shutdown["status"] != "failed"
            shared_shutdown.update(
                status="passed", actual_core_stopped=True,
                queries_after_stop_event=0, new_sender_admitted=False,
            )
            if self.shutdown_late_unsubscribe is not None:
                self.shutdown_late_unsubscribe()
            # Release is demonstrated by exclusive rebinding, not a delay/no-packet guess.
            ports = (7083, receipt["sender_port"])
            reservations = []
            try:
                for port in ports:
                    reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    reservations.append(reservation)
                    reservation.bind(("127.0.0.1", port))
                receipt["released_udp_ports"] = list(ports)
                queries = len(self.queries)
                if self.shutdown_unsubscribe is not None:
                    self.shutdown_unsubscribe()
                    self.shutdown_unsubscribe()
                self.shutdown_interval.close()
                await self.shutdown_interval.async_stop()
                await self.hass.async_block_till_done()
                assert len(self.queries) == queries, "Query emitted after shutdown"
                assert self.shutdown_interval.progress.outcome == "shutdown"
                receipt["queries_after_settled_shutdown"] = len(self.queries) - queries
                receipt["repeated_cleanup_harmless"] = True
            finally:
                for reservation in reservations:
                    reservation.close()
            receipt["error_records"] = self.shutdown_errors
            assert not self.shutdown_errors, "Core shutdown logged errors"
            assert receipt["status"] != "failed", "Shutdown setup failed"
            receipt["status"] = "passed"
        except Exception as err:  # noqa: BLE001
            receipt.update(status="failed", error=f"{type(err).__name__}: {err}")
            self._shared_receipt()["shutdown"].update(
                status="failed", error=f"{type(err).__name__}: {err}"
            )
        finally:
            lifecycle = self.result["discovery_lifecycle"]
            lifecycle["query_observations"] = self.queries[lifecycle["query_start_index"]:]
            receipt["error_records"] = self.shutdown_errors
            if self.shutdown_log_handler is not None:
                logging.getLogger().removeHandler(self.shutdown_log_handler)
                self.shutdown_log_handler = None

    def require_lifecycle(self) -> None:
        """Fail the existing runner only after every finding and cleanup is recorded."""
        receipt = self.result.get("discovery_lifecycle")
        if receipt is None:
            return
        failed = [
            name for name in ("preview_disconnect", "stop_publication", "shutdown")
            if receipt[name]["status"] != "passed"
        ]
        failed.extend(
            f"cleanup.{name}" for name, complete in receipt["cleanup"].items()
            if not complete
        )
        shared = self.result.get("discovery_shared_sender")
        if shared is not None:
            failed.extend(
                f"shared_sender.{name}"
                for name in ("shared_rounds", "recoverable_and_fatal", "shutdown")
                if shared[name]["status"] != "passed"
            )
            failed.extend(
                f"shared_sender.cleanup.{name}"
                for name, complete in shared["cleanup"].items() if not complete
            )
        if failed:
            raise RuntimeError("Discovery lifecycle acceptance failed: " + ", ".join(failed))

    async def close(self) -> None:
        for handle in self.pending_replies:
            handle.cancel()
        self.pending_replies.clear()
        for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        for entry_id in self.added:
            await self.hass.config_entries.async_unload(entry_id)
        for transport in self.transports:
            transport.close()
        for peer in self.peers:
            await peer.close()
            assert not peer.rejected
        if self.network is not None:
            self.network.adapters = self.original_adapters
        self.result.setdefault("discovery", {})["closed"] = True
        if "discovery_lifecycle" in self.result:
            self.result["discovery_lifecycle"]["cleanup"].update(
                pending_fixture_replies_cancelled=not self.pending_replies,
                fixture_udp_transports_closed=all(
                    transport.is_closing() for transport in self.transports
                ),
                fixture_tcp_peers_closed=all(peer.evidence()["closed"] for peer in self.peers),
                fixture_flows_removed=not (
                    self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                ),
                network_inventory_restored=(
                    self.network is None or self.network.adapters is self.original_adapters
                ),
            )
