"""Synthetic loopback LAN for real-Core discovery acceptance; never live access."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

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
        if len(data) != 77 or data[:9] != b"Haier\0\0i\x15":
            raise RuntimeError("Unexpected discovery query")
        self.queries.append(
            {"at": self.hass.loop.time(), "sender_port": addr[1], "bytes": len(data)}
        )
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
        # Cross both removed product deadlines using real production cadence.
        await asyncio.sleep(122)
        assert interval.progress.outcome == "searching"
        assert len(self.queries) == 7
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
            "queries": self.queries,
            "distinct_responders": 6,
            "continuous_seconds": 122,
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
