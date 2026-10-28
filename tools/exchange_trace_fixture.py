"""Native DEBUG/download acceptance inside ha_smoke's existing isolated Core."""

from __future__ import annotations

import asyncio
import gc
import json
import platform
import struct
import sys
import tracemalloc
from collections import deque
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType
from homeassistant.config_entries import ConfigEntry, ConfigEntryState

DOMAIN = "uplus_reborn"
SECOND_DEVICE_ID = "020000000002"
HEADER = struct.Struct(">II32s32sII")
LIMITS = {"operations": 300, "events": 6000, "raw_bytes": 4194304}
SYNTHETIC_SECRET = "fixture-unrelated-cloud-secret-not-for-trace"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class ExchangeTraceFixture:
    """Use real authenticated Core APIs and query-only synthetic appliance peers."""

    def __init__(self, hass: Any, entry: Any, peer: Any, result: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.peer = peer
        self.second_peer = type(peer)(SECOND_DEVICE_ID)
        self.second_entry: Any = None
        self.user: Any = None
        self.refresh: Any = None
        self.session: ClientSession | None = None
        self.ws: ClientWebSocketResponse | None = None
        self.access_token = ""
        self.url = ""
        self.message_id = 0
        self.wire: list[dict[str, str]] = []
        self.result = result
        self.expected_limits: dict[str, dict[str, int]] = {}
        self.receipt: dict[str, Any] = {
            "status": "not_run",
            "scope": "isolated real Core; native logger WebSocket and authenticated diagnostic HTTP download",
            "physical_hardware_verified": False,
            "downloads": [],
            "logger_commands": [],
            "cleanup": {
                "websocket_closed": False, "session_closed": False,
                "token_revoked": False, "user_removed": False,
                "second_entry_unloaded": False, "second_peer_closed": False,
                "peer_hook_removed": False,
            },
        }
        result["exchange_trace"] = self.receipt

    def _traffic(self) -> list[dict[str, int]]:
        return [
            {"connections": peer.connections, "queries": len(peer.queries),
             "controls": len(peer.profile_controls), "rejected": len(peer.rejected)}
            for peer in (self.peer, self.second_peer)
        ]

    async def _message(self) -> dict[str, Any]:
        _require(self.ws is not None, "Native WebSocket was not opened")
        async with asyncio.timeout(10):
            message = await self.ws.receive()
        _require(message.type is WSMsgType.TEXT, "Native WebSocket closed unexpectedly")
        return message.json()

    async def _command(self, command: dict[str, Any]) -> Any:
        _require(self.ws is not None, "Native WebSocket was not opened")
        self.message_id += 1
        await self.ws.send_json({"id": self.message_id, **command})
        response = await self._message()
        _require(response.get("id") == self.message_id and response.get("success") is True,
                 "Native logger WebSocket command failed")
        await self.hass.async_block_till_done()
        return response.get("result")

    async def _level(self, level: str) -> None:
        before = self._traffic()
        await self._command({
            "type": "logger/integration_log_level", "integration": DOMAIN,
            "level": level, "persistence": "none",
        })
        _require(self._traffic() == before, "Logger toggle generated appliance traffic")
        self.receipt["logger_commands"].append({"level": level, "success": True})

    async def _download(self, entry: Any, label: str) -> dict[str, Any]:
        _require(self.session is not None, "HTTP session was not opened")
        before = self._traffic()
        async with self.session.get(
            f"{self.url}/api/diagnostics/config_entry/{entry.entry_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
        ) as response:
            _require(response.status == 200, f"{label}: diagnostic HTTP download failed")
            _require(response.content_type == "application/json",
                     f"{label}: diagnostic response was not JSON")
            _require("attachment;" in response.headers.get("Content-Disposition", ""),
                     f"{label}: response was not a diagnostic attachment")
            body = await response.text()
        _require(self._traffic() == before, f"{label}: download touched an appliance peer")
        for secret in (self.access_token, self.refresh.token, SYNTHETIC_SECRET):
            _require(secret not in body, f"{label}: unrelated secret appeared in download")
        payload = json.loads(body)
        _require(payload["home_assistant"]["version"] == self.result["core_version"],
                 "Diagnostic download reported a different Core version")
        data = payload["data"]
        trace = data["exchange_trace"]
        _require(trace["schema"] == 1
                 and trace["limits"] == self.expected_limits.get(entry.entry_id, LIMITS),
                 f"{label}: unexpected trace schema or retention limits")
        self.receipt["downloads"].append({
            "label": label, "http_status": 200, "authenticated": True,
            "attachment": True, "no_appliance_network": True,
            "capture_state": trace["capture_state"], "retained": trace["retained"],
        })
        return data

    async def _options(
        self, entry: Any, diagnostics: dict[str, Any], *, start: bool,
        controls: bool | None = None,
    ) -> dict[str, Any]:
        """Drive the native authenticated options HTTP flow, not its callback."""
        _require(self.session is not None, "Options HTTP session was not opened")
        before = self._traffic()
        base = f"{self.url}/api/config/config_entries/options/flow"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
            async with self.session.post(url, json=body, headers=headers) as response:
                _require(response.status == 200, "Native options HTTP request failed")
                return await response.json()

        initial = await post(base, {"handler": entry.entry_id})
        _require(initial["type"] == "form" and initial["step_id"] == "init",
                 "Options did not open the settings form")
        values = {"diagnostics": diagnostics}
        if controls is not None:
            values["enable_verified_controls"] = controls
        confirmed = await post(f"{base}/{initial['flow_id']}", values)
        _require(confirmed["type"] == "form" and confirmed["step_id"] == "confirm"
                 and not confirmed.get("errors"), "Options did not reach confirmation")
        saved = await post(f"{base}/{initial['flow_id']}", {"start_capture": start})
        _require(saved["type"] == "create_entry", "Native options did not save/start")
        await self.hass.async_block_till_done()
        _require("start_capture" not in entry.options, "Start consent was persisted")
        if controls is None:
            _require(self._traffic() == before, "Capture preferences/start generated appliance traffic")
        if start:
            self.expected_limits[entry.entry_id] = {
                name: entry.options[f"diagnostic_{name}"]
                for name in ("operations", "events", "raw_bytes")
            }
            logs = await self._command({"type": "logger/log_info"})
            _require(any(row["domain"] == DOMAIN and row["level"] == 10 for row in logs),
                     "Options start did not enable the native DEBUG menu state")
        return confirmed

    async def _reply(
        self, writer: asyncio.StreamWriter, request: bytes, response: bytes,
    ) -> None:
        """Split an unmatched frame, then coalesce its tail and correlated reply."""
        magic, status, source, device, sequence, size = HEADER.unpack(response[:HEADER.size])
        unmatched = HEADER.pack(
            magic, status, source, device, (sequence - 1) & 0xFFFFFFFF, size,
        ) + response[HEADER.size:]
        incoming = unmatched + response
        self.wire.append({"tx": request.hex(), "rx": incoming.hex()})
        writer.write(incoming[:7])
        await writer.drain()
        # Only the synthetic peer is delayed; no production timing is patched.
        await asyncio.sleep(0.05)
        writer.write(incoming[7:])
        await writer.drain()

    async def _poll(self) -> dict[str, int]:
        before = self._traffic()[0]
        await self.entry.runtime_data.async_refresh()
        await self.hass.async_block_till_done()
        _require(self.entry.runtime_data.last_update_success,
                 "Trace fixture authoritative query failed")
        after = self._traffic()[0]
        delta = {key: after[key] - value for key, value in before.items()}
        _require(delta == {"connections": 1, "queries": 1, "controls": 0, "rejected": 0},
                 "Trace fixture did not perform exactly one query-only connection")
        return delta

    @staticmethod
    def _empty(trace: dict[str, Any], state: str) -> None:
        _require(trace["capture_state"] == state and trace["operations"] == []
                 and trace["retained"] == {"operations": 0, "events": 0, "raw_bytes": 0},
                 f"Expected empty {state} trace")

    def _check_exchange(self, trace: dict[str, Any]) -> None:
        _require(trace["capture_state"] == "recording" and len(trace["operations"]) == 1,
                 "Capture did not group a poll as one operation")
        operation = trace["operations"][0]
        _require(operation["partial"] is False and operation["result"] is not None,
                 "Successful fixture poll was incomplete")
        events = operation["events"]
        raw = [event for event in events if event["event"] == "raw"]
        expected = self.wire[-1]
        for direction in ("tx", "rx"):
            chunks = [event for event in raw if event["direction"] == direction]
            actual = "".join(event["data_hex"] for event in chunks)
            _require(actual == expected[direction], f"Captured {direction} bytes differ from real wire")
            _require(all(event["omitted_bytes"] == 0
                         and event["retained_bytes"] == event["byte_length"] for event in chunks),
                     "Small native fixture exchange unexpectedly lost bytes")
            expected_stage = "read" if direction == "rx" else "write_submitted"
            _require(all(event["stage"] == expected_stage for event in chunks),
                     "Raw trace misrepresented transport stage")
        _require(raw[0]["direction"] == "tx", "Raw receive preceded the query submission")
        received = [event for event in raw if event["direction"] == "rx"]
        frame_length = len(bytes.fromhex(expected["rx"])) // 2
        _require(len(received) >= 2 and received[0]["byte_length"] < frame_length,
                 "Native peer did not expose a fragmented frame across reads")
        offset = 0
        coalesced = False
        for event in received:
            coalesced |= offset < frame_length < offset + event["byte_length"]
            offset += event["byte_length"]
        _require(coalesced, "Native peer did not expose multiple frames in one read")
        observations = [event["context"] for event in events if event["event"] == "state_observed"]
        _require([event["decision"] for event in observations] == ["uncorrelated", "correlated"],
                 "Trace lost unmatched/correlated report ordering")
        _require(all(event["decoded_snapshot"] for event in observations),
                 "Trace lost decoded authoritative observations")
        snapshot = self.entry.runtime_data.data
        _require(snapshot.upper.phase == "standby"
                 and snapshot.lower.phase == "standby",
                 "Tracing changed the correlated authoritative state")
        self.receipt["raw_exchange"] = {
            "status": "passed", "trace": trace,
            "wire": expected, "fragmented_frame": True, "coalesced_frames": True,
            "unmatched_then_correlated": True,
        }

    async def _history_depth(self) -> None:
        """Exercise capacity with accelerated real polls, not an elapsed-hour claim."""
        self.peer.query_response_writer = None
        for number in range(1, 361):
            await self._poll()
            if number == 240:
                hour = (await self._download(self.entry, "history_240"))["exchange_trace"]
                _require([op["operation_id"] for op in hour["operations"]] == list(range(1, 241))
                         and hour["omitted"]["operations"] == 0,
                         "Ordinary polls did not retain the first 240 consecutive operations")
        await self._level("WARNING")
        history = (await self._download(self.entry, "history_after_360"))["exchange_trace"]
        operations = history["operations"]
        _require([op["operation_id"] for op in operations] == list(range(61, 361))
                 and history["omitted"]["operations"] == 60,
                 "Operation pressure did not evict only the oldest ordinary polls")
        _require(all(not op["partial"] and op["kind"] == "poll"
                     and op["result"] == {"outcome": "received", "cause": None}
                     for op in operations),
                 "The retained ordinary history lost coverage or changed poll outcomes")
        self.receipt["history_depth"] = {
            "status": "passed", "polls_executed": 360,
            "retained_after_240": hour["retained"],
            "retained_after_360": history["retained"],
            "first_operation": operations[0]["operation_id"],
            "last_operation": operations[-1]["operation_id"],
            "omitted": history["omitted"], "nominal_poll_interval_seconds": 15,
            "nominal_retained_poll_slots_seconds": len(operations) * 15,
            "compact_trace_export_bytes": len(json.dumps(
                history, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")),
            "elapsed_hour_verified": False,
        }
        await self._level("DEBUG")
        self.peer.query_response_writer = self._reply

    @staticmethod
    def _store_measurement(trace: Any) -> dict[str, Any]:
        """Measure retained graph separately from additional export allocations."""
        seen: set[int] = set()
        graph_bytes = raw_object_bytes = 0
        pending = [trace]
        while pending:
            value = pending.pop()
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            size = sys.getsizeof(value)
            graph_bytes += size
            if isinstance(value, bytes):
                raw_object_bytes += size
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set, frozenset, deque)):
                pending.extend(value)
            elif is_dataclass(value) and not isinstance(value, type):
                pending.extend(getattr(value, field.name) for field in fields(value))
            elif hasattr(value, "__dict__"):
                pending.append(vars(value))
        del pending, seen
        gc.collect()
        tracemalloc.start()
        try:
            exported = trace.export()
            encoded = json.dumps(
                exported, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return {
            "retained": exported["retained"], "omitted": exported["omitted"],
            "partial_operations": sum(op["partial"] for op in exported["operations"]),
            "compact_json_bytes": len(encoded),
            "json_metadata_and_framing_bytes": len(encoded) - exported["retained"]["raw_bytes"] * 2,
            "python_store_graph_bytes": graph_bytes,
            "python_raw_object_bytes": raw_object_bytes,
            "python_metadata_container_bytes": graph_bytes - raw_object_bytes,
            "additional_export_json_peak_bytes": peak,
        }

    async def _measure_retention(self) -> dict[str, Any]:
        """Measure actual polls and label direct store stress as synthetic policy IO."""
        from custom_components.uplus_reborn.capture_options import CapturePreferences
        from custom_components.uplus_reborn.exchange_trace import ExchangeTrace

        self.peer.query_response_writer = None
        await self._options(self.entry, {
            "diagnostic_operations": 600, "diagnostic_events": 12000,
            "diagnostic_raw_bytes": 8388608, "diagnostic_duration": "7200",
        }, start=True)
        ordinary = {}
        for number in range(1, 721):
            await self._poll()
            if number in (1, 240, 600, 720):
                ordinary[str(number)] = self._store_measurement(self.entry.runtime_data.trace)
        retained = ordinary["720"]["retained"]
        _require(retained == {"operations": 600, "events": 6000, "raw_bytes": 159600}
                 and ordinary["720"]["omitted"]["operations"] == 120,
                 "Maximum-budget ordinary history lost depth or oldest-first eviction")
        await self._level("WARNING")
        stress = ExchangeTrace()
        maximum = CapturePreferences(600, 12000, 8388608, 7200)
        stress.start(maximum)
        remaining = 8388608
        for operation in range(600):
            stress.begin("synthetic_metadata_pressure", sample=operation)
            for event in range(19):
                stress.event("metadata", values=[
                    f"{operation}:{event}:{column}:" + "x" * 500
                    for column in range(16)
                ])
            size = min(remaining, 13982)
            stress.raw("rx", bytes([operation % 256]) * size, stage="synthetic")
            remaining -= size
            stress.finish("synthetic")
        _require(stress.summary()["retained"] == {
            "operations": 600, "events": 12000, "raw_bytes": 8388608,
        }, "Combined maximum stress failed to fill all selected budgets")
        metadata = self._store_measurement(stress)
        stress.start(maximum)
        stress.begin("synthetic_oversized")
        stress.raw("rx", b"x" * (9 * 1024 * 1024), stage="synthetic")
        stress.finish("synthetic")
        oversized = self._store_measurement(stress)
        _require(oversized["retained"]["raw_bytes"] == 8388608
                 and oversized["omitted"]["raw_bytes"] == 1048576
                 and oversized["partial_operations"] == 1,
                 "One oversized operation escaped the maximum budget or hid loss")
        stress.close()
        return {
            "python_version": platform.python_version(),
            "method": "deduplicated reachable sys.getsizeof graph, not RSS/exclusive ownership; additional tracemalloc export/compact UTF-8 JSON peak after retained store exists",
            "raw_limit_is_total_ram_limit": False,
            "ordinary_real_tcp_polls": ordinary,
            "nominal_retained_poll_slots_seconds": 9000,
            "elapsed_hour_verified": False,
            "synthetic_store_stress_combined_maxima": metadata,
            "synthetic_store_stress_oversized": oversized,
        }

    async def exercise(self, http_port: int) -> None:
        self.receipt["status"] = "running"
        self.url = f"http://127.0.0.1:{http_port}"
        try:
            _require(self.entry.pref_disable_polling,
                     "Trace fixture must disable scheduled polling before entry setup")
            self.receipt["scheduled_polling_excluded"] = True
            self.user = await self.hass.auth.async_create_user(
                "Exchange trace fixture", group_ids=["system-admin"], local_only=True,
            )
            self.refresh = await self.hass.auth.async_create_refresh_token(
                self.user, client_id=f"{self.url}/",
            )
            self.access_token = self.hass.auth.async_create_access_token(self.refresh)
            self.session = ClientSession()
            self.ws = await self.session.ws_connect(f"{self.url}/api/websocket")
            _require((await self._message())["type"] == "auth_required", "Missing Core auth challenge")
            await self.ws.send_json({"type": "auth", "access_token": self.access_token})
            _require((await self._message())["type"] == "auth_ok", "Core WebSocket authentication failed")
            await self._level("WARNING")
            self._empty((await self._download(self.entry, "initial_off"))["exchange_trace"], "stopped")
            self.peer.query_response_writer = self._reply
            off_traffic = await self._poll()
            self._empty((await self._download(self.entry, "off_poll"))["exchange_trace"], "stopped")
            await self._level("DEBUG")
            self._empty((await self._download(self.entry, "enabled_empty"))["exchange_trace"], "recording")
            on_traffic = await self._poll()
            data = await self._download(self.entry, "active_exchange")
            trace = data["exchange_trace"]
            self._check_exchange(trace)
            _require(off_traffic == on_traffic, "Capture changed appliance traffic")
            self.receipt["capture_parity"] = {"status": "passed", "off": off_traffic, "on": on_traffic}
            await self._command({
                "type": "logger/log_level", "module": "exchange_trace_fixture.unrelated",
                "level": "DEBUG", "persistence": "none",
            })
            _require((await self._download(self.entry, "unrelated_logger"))["exchange_trace"] == trace,
                     "Unrelated native logger notification changed trace")
            self.receipt["unrelated_logger_preserved"] = True
            await self._level("WARNING")
            stopped = (await self._download(self.entry, "stopped_first"))["exchange_trace"]
            _require(stopped["capture_state"] == "stopped" and stopped["operations"] == trace["operations"],
                     "Disabling DEBUG did not freeze existing operations")
            repeated = (await self._download(self.entry, "stopped_second"))["exchange_trace"]
            _require(repeated == stopped, "Repeated stopped downloads consumed or changed trace")
            await self._poll()
            _require((await self._download(self.entry, "stopped_after_poll"))["exchange_trace"] == stopped,
                     "Capture-off appliance activity appended trace evidence")
            self.receipt["stopped_downloads_equal"] = True
            await self._level("DEBUG")
            self._empty((await self._download(self.entry, "fresh_capture"))["exchange_trace"], "recording")
            # Populate again, then toggle with no intervening appliance exchange.
            await self._poll()
            await self._level("WARNING")
            before = self._traffic()
            await self._level("DEBUG")
            self._empty((await self._download(self.entry, "off_on_without_exchange"))["exchange_trace"], "recording")
            _require(self._traffic() == before, "OFF/ON reset needed an appliance event")
            self.receipt["off_on_without_exchange_cleared"] = True
            await self._history_depth()
            await self.second_peer.start()
            self.second_entry = ConfigEntry(
                version=1, minor_version=1, domain=DOMAIN, title="Second trace fixture",
                source="user", unique_id=SECOND_DEVICE_ID,
                data={"host": "127.0.0.1", "port": self.second_peer.port,
                      "device_id": SECOND_DEVICE_ID, "cloud_token": SYNTHETIC_SECRET},
                options={"enable_verified_controls": False, "poll_interval": 15},
                pref_disable_polling=True,
                discovery_keys=MappingProxyType({}), subentries_data=(),
            )
            await self.hass.config_entries.async_add(self.second_entry)
            await self.hass.async_block_till_done()
            _require(self.second_entry.state is ConfigEntryState.LOADED, "Second trace entry failed to load")
            self._empty((await self._download(self.second_entry, "second_setup_no_resume"))["exchange_trace"], "stopped")
            await self._options(self.second_entry, {}, start=True)
            await self.second_entry.runtime_data.async_refresh()
            second = (await self._download(self.second_entry, "second_entry"))["exchange_trace"]
            _require(second["capture_state"] == "recording" and len(second["operations"]) == 1,
                     "Explicit second-entry capture did not record its poll")
            self._empty((await self._download(self.entry, "first_entry_isolated"))["exchange_trace"], "recording")
            await self._poll()
            first_data = await self._download(self.entry, "first_entry_own_exchange")
            first = first_data["exchange_trace"]
            _require(all(operation["context"]["device_id"] == self.peer.device_id
                         for operation in first["operations"])
                     and all(operation["context"]["device_id"] == SECOND_DEVICE_ID
                             for operation in second["operations"]),
                     "Domain-wide DEBUG merged different appliance identities")
            _require((await self._download(self.second_entry, "second_entry_unchanged"))["exchange_trace"] == second,
                     "First appliance poll changed the second trace")
            self.receipt["isolated_entries"] = {"status": "passed", "second_trace": second}
            old_coordinator = self.entry.runtime_data
            _require(await self.hass.config_entries.async_unload(self.entry.entry_id), "Trace entry unload failed")
            unloaded = await self._download(self.entry, "unloaded_entry")
            self._empty(unloaded["exchange_trace"], "closed")
            cached_runtime_retained = getattr(self.entry, "runtime_data", None) is old_coordinator
            if cached_runtime_retained:
                _require(unloaded["device"]["state"] == first_data["device"]["state"],
                         "Trace unload changed attached cached non-trace state")
            else:
                _require(unloaded["device"]["state"] is None
                         and unloaded["health"]["freshness"] == "unloaded"
                         and unloaded["health"]["loaded"] is False,
                         "Unloaded diagnostics did not preserve Core's cleared-runtime behavior")
            await self._level("WARNING")
            await self._level("DEBUG")
            self._empty((await self._download(self.entry, "unloaded_logger_change"))["exchange_trace"], "closed")
            _require(await self.hass.config_entries.async_setup(self.entry.entry_id), "Trace entry reload failed")
            await self.hass.async_block_till_done()
            reloaded = (await self._download(self.entry, "reloaded_entry"))["exchange_trace"]
            _require(self.entry.runtime_data is not old_coordinator
                     and reloaded["capture_state"] == "stopped"
                     and not reloaded["operations"],
                     "Reload resumed capture from existing DEBUG without fresh consent")
            _require(old_coordinator.trace.export()["capture_state"] == "closed"
                     and old_coordinator.trace.export()["operations"] == [],
                     "Reload repopulated the old closed capture")
            self.receipt["unload_reload"] = {
                "status": "passed", "closed_empty": True,
                "cached_runtime_retained": cached_runtime_retained,
                "ordinary_unload_behavior_preserved": True, "fresh_capture": reloaded,
            }
            await self._options(self.entry, {
                "diagnostic_operations": 2, "diagnostic_events": 20,
                "diagnostic_raw_bytes": 532, "diagnostic_duration": "1800",
            }, start=True)
            selected = await self._download(self.entry, "selected_limits_start")
            _require(selected["exchange_trace"]["limits"] == {
                "operations": 2, "events": 20, "raw_bytes": 532,
            }, "Explicit limits were not applied")
            active = selected["diagnostic_capture"]
            _require(0 < active["remaining_seconds"] <= 1800
                     and active["started_at"] and active["end_at"],
                     "New capture lacks its selected finite deadline")
            current = self.entry.runtime_data
            await self._options(self.entry, {"diagnostic_operations": 3}, start=False)
            unchanged = await self._download(self.entry, "preferences_only")
            _require(self.entry.runtime_data is current
                     and unchanged["diagnostic_capture"]["end_at"] == active["end_at"]
                     and unchanged["exchange_trace"] == selected["exchange_trace"],
                     "Preference-only Save changed active capture or reloaded")
            await self._level("NOTSET")
            frozen = (await self._download(self.entry, "native_stop_selected"))["exchange_trace"]
            _require(frozen["capture_state"] == "stopped",
                     "Native Disable failed to stop an options capture")
            await self._options(self.entry, {
                "diagnostic_operations": 300, "diagnostic_events": 6000,
                "diagnostic_raw_bytes": 4194304, "diagnostic_duration": "3600",
            }, start=True, controls=True)
            _require(self.entry.runtime_data is not current
                     and self.entry.runtime_data.trace.active,
                     "Same-save control reload lost explicit capture start")
            await self._level("WARNING")
            self.receipt["native_capture_options"] = {
                "status": "passed", "selected_limits": selected["exchange_trace"]["limits"],
                "deadline": active["end_at"], "preference_only_preserved": True,
                "native_stop": True, "same_save_reload_start": True,
                "setup_reload_no_resume": True, "native_menu_debug": True,
            }
            self.receipt["retention_measurements"] = await self._measure_retention()
            self.receipt["status"] = "passed"
        except BaseException as err:
            self.receipt.update(status="failed", error=f"{type(err).__name__}: {err}")
            raise

    async def close(self) -> None:
        """Attempt every owned cleanup even when another cleanup fails."""
        errors = []

        async def finish(name: str, action: Any) -> None:
            try:
                await action()
                self.receipt["cleanup"][name] = True
            except Exception as err:  # noqa: BLE001
                errors.append(f"{name}: {type(err).__name__}: {err}")

        self.peer.query_response_writer = None
        self.receipt["cleanup"]["peer_hook_removed"] = True
        if self.ws is not None:
            await finish("websocket_closed", self.ws.close)
        if self.session is not None:
            await finish("session_closed", self.session.close)
        if self.refresh is not None:
            try:
                self.hass.auth.async_remove_refresh_token(self.refresh)
                self.receipt["cleanup"]["token_revoked"] = True
            except Exception as err:  # noqa: BLE001
                errors.append(f"token_revoked: {type(err).__name__}: {err}")
        if self.user is not None:
            async def remove_user() -> None:
                await self.hass.auth.async_remove_user(self.user)
            await finish("user_removed", remove_user)
        if self.second_entry is not None:
            async def unload_second() -> None:
                coordinator = getattr(self.second_entry, "runtime_data", None)
                if self.second_entry.state is ConfigEntryState.LOADED:
                    _require(await self.hass.config_entries.async_unload(self.second_entry.entry_id),
                             "Second trace entry unload failed")
                if coordinator is not None:
                    self._empty(coordinator.trace.export(), "closed")
            await finish("second_entry_unloaded", unload_second)
        await finish("second_peer_closed", self.second_peer.close)
        self.receipt["second_transport"] = self.second_peer.evidence()
        if self.second_peer.server is not None and not self.receipt["second_transport"]["closed"]:
            errors.append("Second peer did not close")
        self.access_token = ""
        if errors:
            self.receipt["status"] = "failed"
            self.receipt["cleanup_errors"] = errors
            raise RuntimeError("Exchange trace cleanup failed: " + "; ".join(errors))
