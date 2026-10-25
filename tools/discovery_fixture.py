"""Synthetic loopback LAN for real-Core discovery acceptance; never live access."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import asdict
from typing import Any

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
            "queries": list(self.queries),
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
                unchanged_cadence_seconds=20,
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
