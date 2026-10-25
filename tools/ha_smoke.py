#!/usr/bin/env python3
"""Boot an artifact in real Home Assistant against a bounded loopback UWT peer.

Run inside the reproducible Docker Linux container (tools/delivery.py docker)
containing Home Assistant and its integration requirements; this command never
installs dependencies. Example::

    python tools/ha_smoke.py --bundle /tmp/bundle --output /tmp/result.json \
        --scenario execution

The result is software/transport evidence, not appliance or browser acceptance.
The default peer is query-only; the profiles scenario permits one exact synthetic
Spin/400 Start and verifies persistent intents across a second Core instance.
Core base/registries and the required API components run without unrelated default
integrations. The process-local socket guard is not an OS sandbox for untrusted
Python/native code. HTTP binds only to an ephemeral loopback address.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import platform
import signal
import socket
import struct
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

if __package__:
    from .delivery import extract_artifact, load_artifact
else:
    from delivery import extract_artifact, load_artifact

DOMAIN = "uplus_reborn"
DEVICE_ID = "020000000001"
PACKAGE = Path("custom_components") / DOMAIN
HEADER = struct.Struct(">II32s32sII")
BOOT_TIMEOUT = 180
# Discovery adds six real, unshortened shared-sender/fault rounds to its existing
# 122-second collection and issue #62 lifecycle campaign. Other budgets stay put.
DISCOVERY_BOOT_TIMEOUT = BOOT_TIMEOUT + 120
SHUTDOWN_TIMEOUT = 60
CORE_COMPONENTS = (
    "homeassistant",
    "persistent_notification",
    "http",
    "api",
    "websocket_api",
    "config",
    "logger",
    "diagnostics",
)
BOOTSTRAP_COMPONENTS = (*CORE_COMPONENTS, "dhcp", "sensor", "binary_sensor", "frontend", "lovelace")


def _requirements(output: Path) -> None:
    """Export the selected Core's manifest dependency closure without network use."""
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("Install the selected Home Assistant version first")
    components = Path(next(iter(spec.submodule_search_locations))) / "components"
    pending = list(BOOTSTRAP_COMPONENTS)
    seen: set[str] = set()
    requirements: set[str] = set()
    while pending:
        domain = pending.pop()
        if domain in seen:
            continue
        seen.add(domain)
        manifest = json.loads((components / domain / "manifest.json").read_text())
        requirements.update(manifest.get("requirements", ()))
        pending.extend(manifest.get("dependencies", ()))
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(sorted(requirements)) + "\n")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _inner(frame_type: int, payload: bytes) -> bytes:
    """Independent fixture encoder; checksum covers the stuffed body."""
    body = bytes((8 + len(payload), 0, 0, 0, 0, 0, 0, frame_type)) + payload
    body = body.replace(b"\xff", b"\xff\x55")
    checksum = bytes((sum(body) & 0xFF,)).replace(b"\xff", b"\xff\x55")
    return b"\xff\xff" + body + checksum


READ_REQUEST = _inner(0x01, b"\x4d\x01")


def _words(execution: str) -> tuple[int, ...]:
    """Synthesize both drums independently of production decoding helpers."""
    words = [0] * 41
    words[0] = 0x6D01
    words[3] = 0x0003  # Power and physical remote-control mode.
    words[26] = 0x0001
    if execution != "standby":
        for program, phase, remaining, spin in ((1, 3, 5, 15), (24, 26, 28, 32)):
            words[program] = 0x1C00  # Spin.
            words[phase] |= 0x0B20  # Retained Spinning phase and reported lock.
            if execution == "running":
                words[phase] |= 0x0080
            words[remaining] = 0x0600
            words[spin] = 2
    return tuple(words)


class LoopbackGuard:
    """Irrevocable audit hook for this disposable CLI process only."""

    def __init__(self, *, allowed_bind_port: int | None = None) -> None:
        self.blocked: list[dict[str, str]] = []
        self.allowed_connects = 0
        self.allowed_bind_port = allowed_bind_port

    @staticmethod
    def _loopback(host: object) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        if host == "localhost":
            return True
        try:
            address = ipaddress.ip_address(str(host))
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback

    def _deny(self, event: str, reason: str) -> None:
        self.blocked.append({"event": event, "reason": reason})
        raise PermissionError(f"Isolated smoke blocked {event}: {reason}")

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.__new__":
            _, family, kind, _ = args
            allowed = {socket.AF_INET, socket.AF_INET6, socket.AF_UNIX}
            if hasattr(socket, "AF_NETLINK"):
                allowed.add(socket.AF_NETLINK)  # Local interface enumeration.
            if family not in allowed or (
                family in (socket.AF_INET, socket.AF_INET6)
                and kind & 0xF == socket.SOCK_RAW
            ):
                self._deny(event, "raw or non-IP network socket")
        elif event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"):
            sock, address = args[0], args[-1]
            if sock.family not in (socket.AF_INET, socket.AF_INET6):
                return
            # sendmsg on an already connected socket has no destination argument.
            if address is None and event == "socket.sendmsg":
                if (
                    self.allowed_bind_port is not None
                    and sock.getsockname()[1] == self.allowed_bind_port
                ):
                    return
                address = sock.getpeername()
            if not isinstance(address, tuple) or not (
                self._loopback(address[0])
                or (
                    event == "socket.bind"
                    and self.allowed_bind_port is not None
                    and address[0] in ("0.0.0.0", b"0.0.0.0")
                    and address[1] == self.allowed_bind_port
                )
            ):
                self._deny(event, "non-loopback endpoint")
            if event == "socket.connect":
                self.allowed_connects += 1
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            if not self._loopback(args[0]):
                self._deny(event, "non-loopback name resolution")
        elif event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"):
            self._deny(event, "child processes are outside the socket guard")


class ReadOnlyPeer:
    """Bounded TCP framing with exact query-only acceptance and measured replies."""

    def __init__(self, device_id: str = DEVICE_ID) -> None:
        self.device_id = device_id
        self.words = _words("standby")
        self.stage = "standby"
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.queries: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.connections = 0
        self.port = 0
        self.allow_profile_start = False
        self.profile_controls: list[str] = []
        self.profile_initializations = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server = await asyncio.start_server(self._accept, host, port)
        self.port = self.server.sockets[0].getsockname()[1]

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                async with asyncio.timeout(10):
                    try:
                        prefix = await reader.readexactly(8)
                    except asyncio.IncompleteReadError as err:
                        if not err.partial:
                            return
                        raise ValueError("truncated UWT header") from err
                    magic, status = struct.unpack(">II", prefix)
                    if magic != 10004 or status != 0:
                        raise ValueError("non-query UWT envelope")
                    header = prefix + await reader.readexactly(HEADER.size - 8)
                    _, _, source, device, sequence, size = HEADER.unpack(header)
                    if not 0 < size <= (128 if self.allow_profile_start else len(READ_REQUEST)):
                        raise ValueError("unexpected payload length")
                    payload = await reader.readexactly(size)
                    if source != bytes(32) or device.rstrip(b"\0") != self.device_id.encode():
                        raise ValueError("unexpected synthetic peer identity")
                    if payload != READ_REQUEST:
                        writer.write(self._profile_reply(payload, sequence))
                        await writer.drain()
                        continue
                    words = self.words
                    inner = _inner(0x02, struct.pack(">41H", *words))
                    response = HEADER.pack(
                        10005, 0, b"", self.device_id.encode(), sequence, len(inner)
                    ) + inner
                    self.queries.append({
                        "ordinal": len(self.queries) + 1,
                        "stage": self.stage,
                        "sequence": sequence,
                        "at": _utc_now(),
                        "report_words": list(words),
                        "request_sha256": hashlib.sha256(header + payload).hexdigest(),
                        "response_sha256": hashlib.sha256(response).hexdigest(),
                    })
                    writer.write(response)
                    await writer.drain()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as err:
            self.rejected.append(f"{type(err).__name__}: {err}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.writers.discard(writer)

    def _profile_reply(self, payload: bytes, sequence: int) -> bytes:
        """Accept only the one independently specified synthetic Spin/400 Start."""
        if not self.allow_profile_start:
            raise ValueError("non-query payload or invalid checksum")
        if payload == _inner(0x73, b"") and self.profile_initializations == 0:
            self.profile_initializations += 1
            inner = _inner(0x74, bytes.fromhex("0f5a0000000000000000000000000000"))
        elif payload == _inner(0x60, bytes.fromhex(
            "00011c000000000000000190000200000000000000000000"
        )) and self.profile_initializations == 1 and not self.profile_controls:
            self.profile_controls.append(payload.hex())
            self.allow_profile_start = False
            words = list(self.words)
            words[1], words[3], words[5], words[15] = 0x1C00, 0x0BA3, 0x0600, 2
            self.words = tuple(words)
            inner = _inner(0x02, struct.pack(">41H", *words))
        else:
            raise ValueError("unexpected profile control; only one exact Spin/400 is permitted")
        return HEADER.pack(10005, 0, b"", DEVICE_ID.encode(), sequence, len(inner)) + inner

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def evidence(self) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "connections": self.connections,
            "query_count": len(self.queries),
            "control_or_invalid_count": len(self.rejected),
            "queries": self.queries,
            "rejected_requests": self.rejected,
            "profile_start_frames": self.profile_controls,
            "closed": self.server is not None and not self.server.is_serving()
            and not self.tasks and not self.writers,
        }


def _installed_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted((root / PACKAGE).rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Installed artifact contains a link: {path.name}")
        if path.is_dir():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if not path.is_file():
            raise ValueError(f"Installed artifact contains a non-file: {path.name}")
        hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def _exercise_profiles(hass: Any, entry: Any, peer: ReadOnlyPeer, result: dict[str, Any], *, restart: bool) -> None:
    """Exercise real actions/storage, including narrowly scoped adapter failures."""
    from homeassistant.core import Context
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.service import async_get_all_descriptions

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    _require(len(devices) == 1, "Profile fixture must have exactly one owned device")
    device = devices[0]
    c = entry.runtime_data

    async def call(action: str, drum: str = "upper", **data: Any) -> Any:
        return await hass.services.async_call(
            DOMAIN, action, {"device_id": device.id, "drum": drum, **data},
            blocking=True, return_response=action in {"save_profile", "update_profile", "rename_profile"},
        )

    def entity_id(domain: str, key: str) -> str:
        registered = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{DEVICE_ID}_{key}")
        _require(registered is not None, f"Missing native {domain} entity: {key}")
        return registered

    if restart:
        saved = result["saved_profiles"]
        for drum, profile_id in saved.items():
            _require(c.draft(drum) is None and c.selected_profile(drum) is None,
                     "Core restart restored an executable draft/selection")
            _require(hass.states.get(entity_id("text", f"{drum}_profile_name")).state == "",
                     "Core restart restored transient profile name input")
            await call("apply_profile", drum, profile_id=profile_id)
            _require(c.draft(drum) == c.profiles.resolve(profile_id, drum).intent,
                     "Core restart lost the complete saved intent")
        result["profile_restart_persistence"] = True
        c.clear_drafts()
        return

    c.stage_program("upper", "spin")
    c.stage_settings("upper", spin_rpm=400)
    c.stage_program("lower", "dry")
    c.stage_settings("lower", drying_mode="30_min")
    before_queries = len(peer.queries)
    drafts_before_save = {drum: c.draft(drum) for drum in ("upper", "lower")}
    saved = {}
    for drum, name in (("upper", "Spin 400"), ("lower", "Dry 30")):
        await hass.services.async_call("text", "set_value", {
            "entity_id": entity_id("text", f"{drum}_profile_name"), "value": name,
        }, blocking=True)
        await hass.services.async_call("button", "press", {
            "entity_id": entity_id("button", f"{drum}_save_profile"),
        }, blocking=True)
        await hass.async_block_till_done()
        state = hass.states.get(entity_id("select", f"{drum}_profile"))
        saved[drum] = {"profile_id": state.attributes["profiles"][name]}
        _require(c.draft(drum) == drafts_before_save[drum]
                 and c.selected_profile(drum) is None,
                 "Native Save changed a draft or applied the profile")
    _require(len(peer.queries) == before_queries and not peer.profile_controls,
             "Native name/Save contacted the peer")
    upper, lower = saved["upper"], saved["lower"]
    result["saved_profiles"] = {"upper": upper["profile_id"], "lower": lower["profile_id"]}
    before_queries = len(peer.queries)
    expected = c.profiles.resolve(upper["profile_id"], "upper").intent
    c.stage_program("upper", "casual")
    await hass.services.async_call("select", "select_option", {
        "entity_id": entity_id("select", "upper_profile"), "option": "Spin 400",
    }, blocking=True)
    _require(c.draft("upper") == expected, "Apply did not replace the complete draft")
    _require(len(peer.queries) == before_queries and not peer.profile_controls,
             "Apply contacted the peer")
    await call("rename_profile", profile_id=upper["profile_id"], name="Saved spin 400")
    descriptions = await async_get_all_descriptions(hass)
    options = descriptions[DOMAIN]["start_profile"]["fields"]["profile_id"]["selector"]["select"]["options"]
    _require(any(row["value"] == upper["profile_id"] and "Saved spin 400" in row["label"] for row in options),
             "Action editor lacks stable profile choices")

    async def profile_choices() -> dict[str, tuple[tuple[str, str], ...]]:
        descriptions = await async_get_all_descriptions(hass)
        return {
            action: tuple(
                (row["value"], row["label"])
                for row in descriptions[DOMAIN][action]["fields"]["profile_id"]
                ["selector"]["select"]["options"]
            )
            for action in (
                "update_profile", "rename_profile", "delete_profile",
                "apply_profile", "start_profile",
            )
        }

    async def require_choice(profile_id: str, name: str | None) -> None:
        for action, choices in (await profile_choices()).items():
            matching = [label for value, label in choices if value == profile_id]
            _require(
                matching == [] if name is None else len(matching) == 1 and name in matching[0],
                f"{action}: stale native profile choices",
            )

    async def manage_profiles() -> None:
        from custom_components.uplus_reborn import profile_choices as presentation
        from custom_components.uplus_reborn.profiles import ProfileStore

        storage = c.profiles._store
        storage_path = Path(storage.path)
        _require(storage_path == Path(hass.config.path(
            ".storage", f"{DOMAIN}.profiles.{entry.entry_id}",
        )), "Failure fixture is not the entry's actual local HA Store")

        async def disk_bytes() -> bytes:
            return await hass.async_add_executor_job(storage_path.read_bytes)

        async def require_disk_reload() -> None:
            reloaded = ProfileStore(hass, entry.entry_id, DEVICE_ID)
            await reloaded.async_load()
            _require(reloaded.profiles() == c.profiles.profiles(),
                     "A fresh actual HA Store load disagrees with published records")

        initial_records = c.profiles.profiles()
        initial_local = {
            drum: (c.draft(drum), c.selected_profile(drum), c.profile_name(drum))
            for drum in ("upper", "lower")
        }
        response = await call("save_profile", name="Temporary managed spin")
        managed_id = response["profile_id"]
        managed = c.profiles.resolve(managed_id, "upper")
        _require(response == {
            "profile_id": managed_id, "name": "Temporary managed spin",
            "drum": "upper", "revision": 1,
        } and managed.intent == initial_local["upper"][0],
                 "Native Save action did not create the complete revision")
        _require(managed_id not in result["saved_profiles"].values()
                 and all(
                     (c.draft(drum), c.selected_profile(drum), c.profile_name(drum)) == before
                     for drum, before in initial_local.items()
                 ), "Native Save action overwrote, applied or changed local input")
        await hass.async_block_till_done()
        selector_id = entity_id("select", "upper_profile")
        _require(hass.states.get(selector_id).attributes["profiles"].get(managed.name)
                 == managed_id, "Native Save action did not refresh the selector")
        await require_choice(managed_id, managed.name)

        await call("apply_profile", profile_id=managed_id)
        await hass.services.async_call("select", "select_option", {
            "entity_id": entity_id("select", "upper_spin_speed"), "option": "600",
        }, blocking=True)
        updated_intent = c.draft("upper")
        _require(c.selected_profile("upper") is None
                 and updated_intent != managed.intent,
                 "Native manual edit did not detach selected profile identity")
        updated = await call("update_profile", profile_id=managed_id)
        managed = c.profiles.resolve(managed_id, "upper")
        _require(updated == {
            "profile_id": managed_id, "name": managed.name,
            "drum": "upper", "revision": 2,
        } and managed.intent == updated_intent
                 and c.draft("upper") == updated_intent
                 and c.selected_profile("upper") is None,
                 "Native Update changed identity, revision or draft/selection semantics")
        await require_choice(managed_id, managed.name)
        await call("apply_profile", profile_id=managed_id)
        await hass.async_block_till_done()

        # Fail only the native schema adapter, after the real storage commit and
        # listener delivery. The mutation must surface failure, not be replayed.
        class PresentationFailure(RuntimeError):
            """An isolated native action-schema failure after durable mutation."""

        def fail_schema(*_args: Any, **_kwargs: Any) -> None:
            raise PresentationFailure("Isolated native profile presentation failure")

        choices_before = await profile_choices()
        original_schema = presentation.async_set_service_schema
        presentation.async_set_service_schema = fail_schema
        try:
            try:
                await call("rename_profile", profile_id=managed_id, name="Managed renamed")
            except PresentationFailure:
                pass
            else:
                raise RuntimeError("Post-commit presentation failure was hidden")
        finally:
            presentation.async_set_service_schema = original_schema
        await hass.async_block_till_done()
        managed = c.profiles.resolve(managed_id, "upper")
        _require(managed.revision == 3 and managed.name == "Managed renamed"
                 and managed.intent == updated_intent
                 and c.selected_profile("upper") == managed_id
                 and c.draft("upper") == updated_intent,
                 "Post-commit presentation failure rolled back or replayed a mutation")
        selector = hass.states.get(selector_id)
        _require(selector.state == managed.name
                 and selector.attributes["profile_id"] == managed_id
                 and "Temporary managed spin" not in selector.attributes["profiles"],
                 "Committed Rename was not delivered to native selector listeners")
        _require(await profile_choices() == choices_before,
                 "Injected schema failure unexpectedly published action choices")
        durable_after_presentation_failure = await disk_bytes()
        await require_disk_reload()
        # Explicitly repair only presentation, never replay the failed action.
        await presentation.async_refresh_profile_actions(hass)
        await require_choice(managed_id, managed.name)
        _require(await disk_bytes() == durable_after_presentation_failure
                 and c.profiles.resolve(managed_id, "upper") == managed,
                 "Presentation recovery performed another durable mutation")

        records_before_failure = c.profiles.profiles()
        local_before_failure = {
            drum: (c.draft(drum), c.selected_profile(drum), c.profile_name(drum))
            for drum in ("upper", "lower")
        }
        choices_before_failure = await profile_choices()
        selector_before_failure = hass.states.get(selector_id).as_dict()
        actual_path = storage.path
        # Core creates dirname(Store.path) before its WriteError-catching file
        # adapter. A real existing storage file cannot also be that directory,
        # so this yields an actual propagated filesystem commit failure even
        # when the container runs as root. No persistence method is replaced.
        storage.path = str(storage_path / "impossible-commit")
        try:
            try:
                await call("rename_profile", profile_id=managed_id, name="Must not commit")
            except OSError:
                pass
            else:
                raise RuntimeError("Impossible actual HA storage target did not fail")
        finally:
            storage.path = actual_path
        await hass.async_block_till_done()
        _require(c.profiles.profiles() == records_before_failure
                 and all(
                     (c.draft(drum), c.selected_profile(drum), c.profile_name(drum)) == before
                     for drum, before in local_before_failure.items()
                 ), "Failed durable mutation published a record or changed local state")
        _require(hass.states.get(selector_id).as_dict() == selector_before_failure
                 and await profile_choices() == choices_before_failure,
                 "Failed durable mutation changed native selection or choices")
        _require(await disk_bytes() == durable_after_presentation_failure,
                 "Failed durable mutation changed the actual storage file")
        await require_disk_reload()

        await call("delete_profile", profile_id=managed_id)
        await hass.async_block_till_done()
        selector = hass.states.get(selector_id)
        _require(c.draft("upper") == updated_intent
                 and c.selected_profile("upper") is None
                 and selector.state == "none"
                 and selector.attributes["profile_id"] is None
                 and managed.name not in selector.attributes["profiles"],
                 "Native Delete cleared the restored draft or retained selected identity")
        await require_choice(managed_id, None)
        _require(c.profiles.profiles() == initial_records
                 and (c.draft("lower"), c.selected_profile("lower"), c.profile_name("lower"))
                 == initial_local["lower"],
                 "Temporary management changed a retained profile or the other drum")
        await require_disk_reload()
        disk_records = json.loads(await disk_bytes())["data"]["profiles"]
        _require(set(disk_records) == set(result["saved_profiles"].values()),
                 "Temporary management left unexpected durable profile IDs")
        result["profile_management"] = {
            "native_action_save": True,
            "updated_stable_id_revision": 2,
            "post_commit_failure_durable_revision": 3,
            "post_commit_failure_propagated": True,
            "presentation_recovered_without_mutation_replay": True,
            "filesystem_commit_failure_preserved_all_state": True,
            "real_store_reload_after_success_and_failure": True,
            "delete_preserved_draft_and_cleared_selection": True,
            "native_selector_and_all_action_choices_current": True,
            "temporary_profile_removed": managed_id,
        }

    # Cancel the scheduled poll through public coordinator APIs before measuring
    # local operations, then restore its interval. Polling is not mutation traffic.
    poll_interval = c.update_interval
    c.update_interval = None
    c.async_set_updated_data(c.data)
    try:
        await hass.async_block_till_done()
        management_queries = len(peer.queries)
        authoritative_before_management = c.data
        await manage_profiles()
        _require(len(peer.queries) == management_queries
                 and not peer.profile_controls and peer.profile_initializations == 0,
                 "Local profile management dispatched a peer read or control")
        _require(c.data == authoritative_before_management,
                 "Local profile management changed authoritative appliance state")
        result["profile_management"]["operation_peer_queries"] = 0
        result["profile_management"]["scheduled_polling_excluded"] = True
    finally:
        c.update_interval = poll_interval
        c.async_set_updated_data(c.data)
    for drum, profile_id in (("upper", "missing"), ("lower", upper["profile_id"])):
        try:
            await call("start_profile", drum, profile_id=profile_id)
        except ServiceValidationError:
            pass
        else:
            raise RuntimeError("Invalid profile start was accepted")
    # A valid profile must still reject fresh unsafe appliance state with no write.
    words = list(peer.words)
    words[3] &= ~2
    peer.words = tuple(words)
    await c.async_refresh()
    try:
        await call("start_profile", profile_id=upper["profile_id"])
    except ServiceValidationError:
        pass
    else:
        raise RuntimeError("Profile Start bypassed physical Remote Control")
    _require(not peer.profile_controls, "Invalid profile/state dispatched a control")
    peer.words = _words("standby")
    await c.async_refresh()
    c.stage_program("upper", "casual")
    drafts = {drum: c.draft(drum) for drum in ("upper", "lower")}
    peer.allow_profile_start = True
    await Script(
        hass,
        [{"action": f"{DOMAIN}.start_profile", "data": {
            "device_id": device.id, "drum": "upper", "profile_id": upper["profile_id"],
        }}],
        "Saved profile smoke",
        DOMAIN,
    ).async_run(context=Context())
    _require(len(peer.profile_controls) == 1, "Profile Start was not dispatched exactly once")
    _require(c.health.latest_command.outcome == "confirmed", "Profile Start was not confirmed")
    _require(all(c.draft(drum) == draft for drum, draft in drafts.items()), "Start changed interactive drafts")
    _require(await hass.config_entries.async_reload(entry.entry_id), "Profile reload failed")
    await hass.async_block_till_done()
    c = entry.runtime_data
    for drum, profile_id in result["saved_profiles"].items():
        _require(c.draft(drum) is None and c.selected_profile(drum) is None, "Reload restored selection")
        _require(hass.states.get(entity_id("text", f"{drum}_profile_name")).state == "",
                 "Reload restored transient profile name input")
        _require(c.profiles.resolve(profile_id, drum).intent is not None, "Reload lost a profile")
    result["profile_acceptance"] = {
        "apply_local_only": True, "invalid_ids_and_state_rejected": True,
        "native_name_save_restore": True, "save_local_only": True,
        "transient_names_cleared": True,
        "stable_action_choices": True, "start_exact_spin_400_once": True,
        "drafts_independent": True, "reload_persistence": True,
        "native_action_management_and_failures": True,
    }


async def _exercise(
    root: Path,
    scenario: str,
    result: dict[str, Any],
    *,
    language: str = "en",
    serve: bool = False,
    serve_timeout: float = 300.0,
    serve_port: int | None = None,
    record_history: bool = False,
    after_scenario: Callable[[Any, Any, ReadOnlyPeer], Awaitable[None]] | None = None,
) -> None:
    # Imports are intentionally delayed until the immutable artifact is installed
    # and the guard is active. Never import the checkout's custom component.
    from homeassistant import bootstrap, loader
    from homeassistant.config_entries import (
        ConfigEntries,
        ConfigEntry,
        ConfigEntryState,
    )
    from homeassistant.const import __version__ as core_version
    from homeassistant.core import CoreState, HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    result["core_version"] = core_version
    peer = ReadOnlyPeer()
    hass = HomeAssistant(str(root))
    entry = None
    user = None
    refresh = None
    signal_handlers_installed = False
    discovery_fixture = None
    result["shutdown"] = {"entry_unloaded": False, "core_stopped": False}
    try:
        async with asyncio.timeout(
            DISCOVERY_BOOT_TIMEOUT if scenario == "discovery" else BOOT_TIMEOUT
        ):
            await peer.start()
            loader.async_setup(hass)
            hass.config.skip_pip = True
            # Core rejects port zero. Select a vacant loopback port; any competing
            # bind fails acceptance rather than falling back to a wildcard listener.
            if serve_port is not None:
                http_port = serve_port
                http_host = "0.0.0.0"
            else:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    http_port = reservation.getsockname()[1]
                http_host = "127.0.0.1"
            config = {
                "homeassistant": {
                    "name": "Uplus isolated smoke", "latitude": 0, "longitude": 0,
                    "elevation": 0, "unit_system": "metric", "time_zone": "UTC",
                    "country": "GB",
                },
                "http": {"server_host": http_host, "server_port": http_port},
            }
            hass.config_entries = ConfigEntries(hass, config)
            frontend_loaded = False
            if serve and importlib.util.find_spec("hass_frontend") is not None:
                storage = root / ".storage"
                storage.mkdir(exist_ok=True)
                (storage / "onboarding").write_text(
                    json.dumps({
                        "version": 4,
                        "minor_version": 1,
                        "key": "onboarding",
                        "data": {"done": ["user", "core_config", "integration", "analytics"]},
                    }),
                    encoding="utf-8",
                )
            await loader.async_get_custom_components(hass)
            base_loaded = await bootstrap.async_load_base_functionality(hass)
            # Minimum Core returns None on success; newer Core can return False.
            _require(base_loaded is not False, "Core registry/base initialization failed")
            await bootstrap.async_process_ha_core_config(hass, config["homeassistant"])
            # Entity names use the Core language at registration, not the browser locale.
            hass.config.language = language
            result["language"] = hass.config.language
            if record_history:
                from homeassistant.helpers.recorder import async_initialize_recorder

                async_initialize_recorder(hass)
                config["recorder"] = {"commit_interval": 1, "auto_purge": False}
                _require(await async_setup_component(hass, "recorder", config),
                         "Recorder failed to load before entity listeners")
            for domain in CORE_COMPONENTS:
                _require(await async_setup_component(hass, domain, config),
                         f"Core component {domain} failed (check fixture requirements)")
            if serve and importlib.util.find_spec("hass_frontend") is not None:
                for domain in ("frontend", "lovelace"):
                    _require(await async_setup_component(hass, domain, config),
                             f"Core UI component {domain} failed")
                frontend_loaded = True
            if serve and not frontend_loaded:
                raise RuntimeError("The reproducible image is missing frontend requirements for --serve")
            if serve and scenario.startswith("profiles"):
                for domain in ("script", "automation"):
                    _require(await async_setup_component(hass, domain, config),
                             f"Core profile editor component {domain} failed")
            result["bootstrap_scope"] = "base registries and explicit core/API components"
            await hass.async_start()
            _require(hass.state is CoreState.running, "Core did not reach running")
            required = {"http", "config", "websocket_api"}
            result["required_components"] = sorted(required & hass.config.components)
            _require(required <= hass.config.components, "Core API dependencies failed to load")
            result["core_started"] = True
            # Add after Core startup: the DHCP dependency's startup-only network
            # discovery is outside this manually routed fixture's scope. Its real
            # setup still runs; no production read, entity or coordinator is mocked.
            if scenario == "offline":
                # Keep the known endpoint but refuse the very first connection.
                await peer.close()
            if scenario == "profiles_restart":
                entry = hass.config_entries.async_get_entry(result["profile_entry_id"])
                _require(entry is not None, "Core restart lost the persisted config entry")
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "host": "127.0.0.1", "port": peer.port}
                )
                _require(await hass.config_entries.async_setup(entry.entry_id),
                         "Persisted profile entry failed to set up")
            else:
                entry = ConfigEntry(
                    version=1, minor_version=1, domain=DOMAIN,
                    title="Uplus isolated smoke", source="user", unique_id=DEVICE_ID,
                    data={"host": "127.0.0.1", "port": peer.port, "device_id": DEVICE_ID},
                    options={"enable_verified_controls": scenario.startswith("profiles") or scenario == "offline", "poll_interval": 15},
                    discovery_keys=MappingProxyType({}), subentries_data=(),
                )
                await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            result["entry_state"] = entry.state.value
            _require(entry.state is ConfigEntryState.LOADED, "Synthetic entry did not load")
            coordinator = entry.runtime_data
            if scenario != "offline":
                _require(coordinator.last_update_success, "Initial authoritative read failed")
            imported = sys.modules[f"custom_components.{DOMAIN}"]
            imported_path = Path(imported.__file__).resolve()
            _require(imported_path == (root / PACKAGE / "__init__.py").resolve(),
                     "Core imported integration outside the installed artifact")
            result["artifact_import_verified"] = True
            registry = er.async_get(hass)
            registered = er.async_entries_for_config_entry(registry, entry.entry_id)
            result["registered_entity_count"] = len(registered)
            sensor_ids = {
                row.unique_id.removeprefix(f"{DEVICE_ID}_"): row.entity_id
                for row in registered if row.domain == "sensor" and row.platform == DOMAIN
            }

            if scenario == "offline":
                _require(coordinator.data is None and not coordinator.last_update_success,
                         "Offline startup fabricated an authoritative snapshot")
                _require(not peer.queries, "Offline startup unexpectedly reached a peer")
                identities = {(row.domain, row.platform, row.unique_id): (row.id, row.entity_id)
                              for row in registered}
                power_id = next(row.entity_id for row in registered
                                if row.domain == "binary_sensor"
                                and row.unique_id == f"{DEVICE_ID}_power")
                start_id = next(row.entity_id for row in registered
                                if row.domain == "button"
                                and row.unique_id == f"{DEVICE_ID}_upper_start")
                _require(hass.states.get(power_id).state == "unavailable"
                         and hass.states.get(start_id).state == "unavailable",
                         "Offline initial state or command was available")
                result["offline_startup"] = {
                    "entry_state": entry.state.value,
                    "snapshot_absent": coordinator.data is None,
                    "power_state": hass.states.get(power_id).state,
                    "start_state": hass.states.get(start_id).state,
                    "transitions": [],
                }
                for online in (True, False, True):
                    updated = asyncio.Event()

                    def on_update(
                        connected: bool = online, signal: asyncio.Event = updated,
                    ) -> None:
                        if coordinator.last_update_success is connected:
                            signal.set()

                    unsubscribe = coordinator.async_add_listener(on_update)
                    started = time.monotonic()
                    try:
                        if online:
                            await peer.start(port=peer.port)
                        else:
                            await peer.close()
                        # No manual refresh: production interval polling owns recovery.
                        async with asyncio.timeout(35):
                            await updated.wait()
                        await hass.async_block_till_done()
                    finally:
                        unsubscribe()
                    _require(entry.state is ConfigEntryState.LOADED
                             and entry.runtime_data is coordinator,
                             "Connectivity change replaced or unloaded the entry")
                    _require(hass.states.get(power_id).state ==
                             ("on" if online else "unavailable"),
                             "Connectivity change left the wrong entity availability")
                    result["offline_startup"]["transitions"].append({
                        "connected": online, "elapsed_seconds": time.monotonic() - started,
                        "power_state": hass.states.get(power_id).state,
                        "query_count": len(peer.queries),
                    })
                _require(identities == {
                    (row.domain, row.platform, row.unique_id): (row.id, row.entity_id)
                    for row in er.async_entries_for_config_entry(registry, entry.entry_id)
                }, "Offline recovery changed entity identities")
                result["offline_startup"]["identities_preserved"] = True

            async def observe(label: str, expected: str, *, refresh: bool) -> None:
                before = len(peer.queries)
                if refresh:
                    peer.stage = label
                    peer.words = _words(expected)
                    await coordinator.async_refresh()
                    await hass.async_block_till_done()
                    _require(len(peer.queries) > before, f"{label}: no real TCP query")
                _require(coordinator.last_update_success, f"{label}: coordinator read failed")
                states = {}
                assertions = []
                for drum in ("upper", "lower"):
                    for key, value in (
                        (f"{drum}_phase", "standby" if expected == "standby" else "spinning"),
                        (f"{drum}_execution_state", expected),
                    ):
                        entity_id = sensor_ids.get(key)
                        state = hass.states.get(entity_id) if entity_id else None
                        actual = state.state if state else None
                        assertions.append({"key": key, "expected": value, "actual": actual})
                        _require(actual == value, f"{label}: {key} expected {value}, got {actual}")
                for key, entity_id in sorted(sensor_ids.items()):
                    state = hass.states.get(entity_id)
                    if state is not None:
                        states[key] = state.as_dict()
                result["observations"].append({
                    "stage": label, "at": _utc_now(), "query_count": len(peer.queries),
                    "assertions": assertions, "sensor_states": states,
                    "authoritative_snapshot": asdict(coordinator.data),
                })

            _require(bool(peer.queries), "No initial read-only query reached the peer")
            await observe("standby", "standby", refresh=False)
            if scenario == "execution":
                for label, state in (("running_initial", "running"), ("paused", "paused"),
                                     ("running_resumed", "running")):
                    await observe(label, state, refresh=True)
            if scenario.startswith("profiles"):
                result["profile_entry_id"] = entry.entry_id
                await _exercise_profiles(hass, entry, peer, result, restart=scenario == "profiles_restart")
                coordinator = entry.runtime_data
            if serve or scenario == "discovery":
                # Current Core does not allow removing the first, owner account.
                await hass.auth.async_create_user("Fixture owner", local_only=True)
            if scenario in ("discovery", "discovery-identification"):
                if __package__:
                    from .discovery_fixture import DiscoveryFixture
                else:
                    from discovery_fixture import DiscoveryFixture
                discovery_fixture = DiscoveryFixture(hass, ReadOnlyPeer, result)
                identification = scenario == "discovery-identification"
                await discovery_fixture.start(identification=identification)
                if identification:
                    await discovery_fixture.exercise_identification()
                else:
                    await discovery_fixture.exercise()
                    await discovery_fixture.exercise_lifecycle(http_port)
                    await discovery_fixture.exercise_shared_sender()
            if after_scenario is not None:
                await after_scenario(hass, entry, peer)
            _require(not peer.rejected, "Peer rejected a control or malformed request")
        if serve:
            user = await hass.auth.async_create_user(
                "Uplus isolated smoke admin",
                group_ids=["system-admin"],
                local_only=True,
            )
            url = f"http://127.0.0.1:{http_port}"
            refresh = await hass.auth.async_create_refresh_token(
                user, client_id=f"{url}/"
            )
            access_token = hass.auth.async_create_access_token(refresh)
            serve_info: dict[str, Any] = {
                "url": url,
                "entry_id": entry.entry_id,
                "frontend_loaded": frontend_loaded,
                "token_revoked": False,
                "user_removed": False,
                "stopped_by": None,
                "commands": [],
            }
            result["serve"] = serve_info
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
            fd = sys.stdin.fileno()
            buffer = bytearray()
            reader_active = False

            def _on_stdin() -> None:
                nonlocal reader_active
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    if reader_active:
                        loop.remove_reader(fd)
                        reader_active = False
                    if buffer:
                        tail = buffer.decode("utf-8", "replace").strip()
                        buffer.clear()
                        if tail:
                            queue.put_nowait(("line", tail))
                    queue.put_nowait(("eof", None))
                    return
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, rest = buffer.partition(b"\n")
                    buffer.clear()
                    buffer.extend(rest)
                    queue.put_nowait(("line", raw_line.decode("utf-8", "replace").strip()))

            def _on_signal(sig_name: str) -> None:
                queue.put_nowait(("signal", sig_name))

            loop.add_reader(fd, _on_stdin)
            reader_active = True
            for sig, sig_name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
                loop.add_signal_handler(sig, _on_signal, sig_name)
            signal_handlers_installed = True
            print(
                json.dumps({
                    "event": "serving",
                    "url": url,
                    "entry_id": entry.entry_id,
                    "scenario": scenario,
                    "frontend_loaded": frontend_loaded,
                    "access_token": access_token,
                    "refresh_token": refresh.token,
                }),
                flush=True,
            )
            try:
                async with asyncio.timeout(serve_timeout):
                    while True:
                        kind, value = await queue.get()
                        if kind == "eof":
                            serve_info["stopped_by"] = "eof"
                            serve_info["commands"].append("eof")
                            break
                        if kind == "signal":
                            serve_info["stopped_by"] = value
                            serve_info["commands"].append(f"signal:{value}")
                            break
                        command = value or ""
                        if not command:
                            continue
                        if command in {"stop", "quit", "exit"}:
                            serve_info["stopped_by"] = "command"
                            serve_info["commands"].append("stop")
                            break
                        _require(
                            command in {"standby", "running", "paused", "offline", "online"},
                            f"Unsupported --serve command: {command!r}",
                        )
                        serve_info["commands"].append(command)
                        stage_label = f"serve_{len(serve_info['commands'])}_{command}"
                        if command == "offline":
                            await peer.close()
                        elif command == "online":
                            await peer.start(port=peer.port)
                        else:
                            await observe(stage_label, command, refresh=True)
                        print(
                            json.dumps({
                                "event": "stage",
                                "stage": command,
                                "query_count": len(peer.queries),
                            }),
                            flush=True,
                        )
            except TimeoutError:
                serve_info["stopped_by"] = "timeout"
                raise
            finally:
                if reader_active:
                    loop.remove_reader(fd)
            _require(not peer.rejected, "Peer rejected a control or malformed request")
    finally:
        try:
            try:
                async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                    if discovery_fixture is not None:
                        if "discovery_lifecycle" in result:
                            # Arm only at the existing final teardown. Do not fake a
                            # stop event or abort the active interval before Core stops.
                            await discovery_fixture.prepare_shutdown()
                        else:
                            await discovery_fixture.close()
                    if refresh is not None:
                        hass.auth.async_remove_refresh_token(refresh)
                        if "serve" in result:
                            result["serve"]["token_revoked"] = True
                    if user is not None:
                        await hass.auth.async_remove_user(user)
                        if "serve" in result:
                            result["serve"]["user_removed"] = True
                    if entry is not None and entry.state is ConfigEntryState.LOADED:
                        result["shutdown"]["entry_unloaded"] = (
                            await hass.config_entries.async_unload(entry.entry_id)
                        )
            finally:
                try:
                    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                        await hass.async_stop(force=True)
                        result["shutdown"]["core_stopped"] = hass.state is CoreState.stopped
                        result["shutdown"]["core_exit_code"] = hass.exit_code
                finally:
                    if discovery_fixture is not None and "discovery_lifecycle" in result:
                        try:
                            await discovery_fixture.finish_shutdown(
                                core_stopped=hass.state is CoreState.stopped
                            )
                        finally:
                            await discovery_fixture.close()
        finally:
            await peer.close()
            result["transport"] = peer.evidence()
            if signal_handlers_installed:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
    _require(result["shutdown"]["entry_unloaded"], "Entry failed to unload")
    _require(result["shutdown"]["core_stopped"] and hass.exit_code == 0,
             "Core did not shut down cleanly")
    _require(result["transport"]["closed"], "Loopback peer did not close cleanly")
    _require(not peer.rejected, "Shutdown emitted a control or malformed request")
    if discovery_fixture is not None:
        discovery_fixture.require_lifecycle()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot encode evidence value {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path,
                        help="Write selected Core manifest requirements to a new file; no boot/network")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path,
                        help="New JSON result file (existing files are never overwritten)")
    parser.add_argument(
        "--scenario",
        choices=("standby", "execution", "profiles", "discovery", "discovery-identification", "offline"),
        default="standby",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Core language before entity registration (default: en); select the frontend locale separately",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Keep isolated Core serving on loopback with disposable local auth until stdin stop/EOF",
    )
    parser.add_argument(
        "--serve-timeout",
        type=float,
        default=300.0,
        help="Maximum seconds to keep --serve active (default: 300)",
    )
    args = parser.parse_args()
    if args.requirements:
        try:
            _requirements(args.requirements)
        except (OSError, RuntimeError, ValueError) as err:
            parser.error(str(err))
        return 0
    if args.bundle is None or args.output is None:
        parser.error("--bundle and --output are required for a smoke run")
    if args.serve_timeout <= 0:
        parser.error("--serve-timeout must be positive")
    started = time.monotonic()
    result: dict[str, Any] = {
        "schema": 1, "scenario": args.scenario, "started_at": _utc_now(),
        "python_version": platform.python_version(), "platform": platform.platform(),
        "success": False, "observations": [], "temporary_config_removed": False,
    }
    raw_serve_port = os.environ.get("UPLUS_SMOKE_CONTAINER_SERVE_PORT", "").strip()
    serve_port = int(raw_serve_port) if (args.serve and raw_serve_port) else None
    if serve_port is not None and not (1 <= serve_port <= 65535):
        parser.error("UPLUS_SMOKE_CONTAINER_SERVE_PORT must be between 1 and 65535")
    guard = LoopbackGuard(allowed_bind_port=serve_port)
    logging.basicConfig(level=logging.WARNING)
    try:
        # Reserve the result before booting so an invalid output cannot hide a run.
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            try:
                manifest = load_artifact(args.bundle)
                result["artifact"] = manifest
                _require(sys.platform == "linux", "Real Home Assistant smoke requires the Linux Docker container")
                sys.addaudithook(guard.audit)
                with tempfile.TemporaryDirectory(prefix="uplus-ha-smoke-") as temporary:
                    root = Path(temporary)
                    installed = extract_artifact(args.bundle, root)
                    _require(installed == manifest, "Artifact changed between validation and extraction")
                    before = _installed_hashes(root)
                    _require(before == manifest["files"], "Installed member set or hashes differ")
                    result["installed_hashes_before"] = before
                    # HA's custom-component loader uses the fresh config directory.
                    sys.path.insert(0, str(root))
                    try:
                        asyncio.run(
                            _exercise(
                                root,
                                args.scenario,
                                result,
                                language=args.language,
                                serve=args.serve,
                                serve_timeout=args.serve_timeout,
                                serve_port=serve_port,
                            )
                        )
                        if args.scenario == "profiles":
                            restarted = {
                                "observations": [], "saved_profiles": result["saved_profiles"],
                                "profile_entry_id": result["profile_entry_id"],
                            }
                            asyncio.run(_exercise(root, "profiles_restart", restarted, language=args.language))
                            result["restart"] = restarted
                    finally:
                        sys.path.remove(str(root))
                        after = _installed_hashes(root)
                        result["installed_hashes_after"] = after
                        _require(after == manifest["files"], "Runtime artifact hashes changed")
                result["success"] = True
            except (Exception, KeyboardInterrupt) as err:  # noqa: BLE001
                result["error"] = f"{type(err).__name__}: {err}"
            finally:
                if "temporary" in locals():
                    result["temporary_config_removed"] = not Path(temporary).exists()
                result["network_guard"] = {
                    "scope": "Python socket audit; child processes and raw IP sockets denied",
                    "allowed_loopback_connects": guard.allowed_connects,
                    "blocked": guard.blocked,
                }
                result["finished_at"] = _utc_now()
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                json.dump(result, output, indent=2, default=_json_default)
                output.write("\n")
    except OSError as err:
        print(f"ha_smoke: {err}", file=sys.stderr)
        return 1
    if not result["success"]:
        print(f"ha_smoke: {result.get('error', 'failed')}", file=sys.stderr)
        return 1
    print(f"ha_smoke: {args.scenario} passed; evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
