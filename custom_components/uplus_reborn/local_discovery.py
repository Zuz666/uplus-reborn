"""UI-owned legacy UWT discovery; no appliance controls or runtime recovery."""

from __future__ import annotations

import asyncio
import socket
import struct
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from homeassistant.components.network import async_get_adapters
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.singleton import singleton

from .const import DOMAIN, SUPPORTED_WIFI_TYPE

DISCOVERY_PORT = 7083
QUERY = (
    b"Haier"
    + struct.pack(">I", 0x6915)
    + bytes(8)
    + struct.pack(">I", 56)
    + bytes(16)
    + b"2.0.0\0\0\0"
    + b"UDISCOVERY_SDK".ljust(32, b"\0")
)


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    """Source-established legacy record, independent of support eligibility."""

    device_id: str
    type_id: str
    host: str
    advertised_host: str
    security: int
    busy: bool
    ready_to_bind: bool
    last_seen: datetime
    conflict: bool = False
    alternatives: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        """Only the exact target and plaintext legacy transport are supported."""
        return self.type_id == SUPPORTED_WIFI_TYPE and self.security == 0


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Immutable observations and explicit receive/send coverage failures."""

    records: tuple[DiscoveryRecord, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScanEvent:
    """One bounded, typed observation; presentation and localization stay in HA."""

    elapsed: int
    kind: str
    detail: str = ""
    record: DiscoveryRecord | None = None


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """Immutable per-dialog activity, not an acknowledgement or packet dump."""

    elapsed: int
    rounds: int
    sent: int
    replies: int
    devices: int
    events: tuple[ScanEvent, ...]
    issues: tuple[str, ...]
    outcome: str


class ScanActivity:
    """Own counters and the explicit stop boundary for one search interval."""

    def __init__(self) -> None:
        self.started = asyncio.get_running_loop().time()
        self.stop = asyncio.Event()
        self.rounds = self.sent = self.replies = self.devices = 0
        self.events: deque[ScanEvent] = deque(maxlen=12)
        self.issues: list[str] = []
        self.outcome = "searching"
        self.finished_elapsed: int | None = None

    def event(
        self, kind: str, detail: str = "", record: DiscoveryRecord | None = None
    ) -> None:
        """Bound history; combine consecutive reply bursts without losing counts."""
        elapsed = int(asyncio.get_running_loop().time() - self.started)
        if kind == "replies" and self.events and self.events[-1].kind == kind:
            previous = self.events.pop()
            detail = str(int(previous.detail) + 1)
        self.events.append(ScanEvent(elapsed, kind, detail, record))

    def snapshot(self) -> ScanProgress:
        """Copy only the bounded presentation data at the UI refresh cadence."""
        elapsed = (
            self.finished_elapsed
            if self.finished_elapsed is not None
            else int(asyncio.get_running_loop().time() - self.started)
        )
        return ScanProgress(
            elapsed,
            self.rounds,
            self.sent,
            self.replies,
            self.devices,
            tuple(self.events),
            tuple(self.issues),
            self.outcome,
        )

    def finish(self, outcome: str) -> None:
        """Freeze elapsed time and terminal outcome once."""
        if self.finished_elapsed is None:
            self.event(outcome)
            self.finished_elapsed = int(
                asyncio.get_running_loop().time() - self.started
            )
            self.outcome = outcome


def decode_record(
    data: bytes, source: str, networks: tuple[IPv4Network, ...]
) -> DiscoveryRecord | None:
    """Decode the SDK's fixed 288-byte legacy body, not arbitrary variants.

    uSDK 4.0.9's 0x684d handler copies 288 bytes regardless of type. Its
    0x65f4 handler copies the same named slots through body offset 239;
    that source-only variant is deliberately not accepted as a 0x684d packet.
    """
    if len(data) != 309 or data[:5] != b"Haier":
        return None
    if (
        struct.unpack_from(">I", data, 5)[0] != 0x684D
        or struct.unpack_from(">I", data, 17)[0] != 288
    ):
        return None
    flags = struct.unpack_from(">H", data, 9)[0]
    if flags & ~15:
        return None
    try:
        identity_slot = data[21:37]
        identity, _, padding = identity_slot.partition(b"\0")
        if padding.strip(b"\0") or not 1 <= len(identity) <= 16:
            return None
        device_id = identity.decode("ascii").upper()
        if not all(c.isalnum() or c in "-_" for c in device_id):
            return None
        address_slot = data[229:245]
        address, separator, padding = address_slot.partition(b"\0")
        if not separator or padding.strip(b"\0"):
            return None
        advertised = IPv4Address(address.decode("ascii"))
        observed = IPv4Address(source)
        if advertised != observed or observed.is_multicast or observed.is_unspecified:
            return None
        if not any(
            observed in network
            and observed not in (network.network_address, network.broadcast_address)
            for network in networks
        ):
            return None
    except (UnicodeError, ValueError):
        return None
    return DiscoveryRecord(
        device_id,
        data[37:69].hex(),
        str(observed),
        str(advertised),
        (flags >> 1) & 3,
        bool(flags & 1),
        bool(flags & 8),
        datetime.now(UTC),
    )


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, owner: DiscoveryCollector) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner.receive(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        self.owner.issue("receive_failed")


@dataclass(eq=False, slots=True)
class _Search:
    """Keep one collector registration's observations and activity together."""

    records: dict[str, DiscoveryRecord]
    activity: ScanActivity


class DiscoveryCollector:
    """Share sending and sockets while retaining independent observation windows."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._lock = asyncio.Lock()
        self._searches: list[_Search] = []
        self._issues: list[str] = []
        self._networks: tuple[IPv4Network, ...] = ()
        self._senders: list[tuple[asyncio.DatagramTransport, str]] = []
        self._transports: list[asyncio.DatagramTransport] = []
        self._sender_task: asyncio.Task[None] | None = None

    def issue(self, issue: str) -> None:
        """Retain bounded coverage diagnostics without remote identifiers."""
        if issue not in self._issues:
            self._issues.append(issue)
        for search in self._searches:
            activity = search.activity
            if not activity.stop.is_set() and issue not in activity.issues:
                activity.issues.append(issue)
                activity.event("issue", issue)

    def receive(self, data: bytes, source: str) -> None:
        """Fan out observations to only currently active scans."""
        record = decode_record(data, source, self._networks)
        if record is None:
            return
        for search in self._searches:
            records, activity = search.records, search.activity
            if activity.stop.is_set():
                continue
            activity.replies += 1
            if record.device_id not in records:
                activity.event("found", record=record)
            else:
                activity.event("replies", "1")
            previous = records.get(record.device_id)
            if previous and (
                previous.conflict
                or (previous.host, previous.type_id, previous.security)
                != (record.host, record.type_id, record.security)
            ):
                evidence = f"{record.device_id} / {record.host} / {record.type_id} / {record.security}"
                alternatives = tuple(dict.fromkeys((*previous.alternatives, evidence)))[
                    :8
                ]
                record_for_scan = replace(
                    previous,
                    conflict=True,
                    last_seen=record.last_seen,
                    alternatives=alternatives,
                )
            else:
                record_for_scan = record
            # One address cannot safely identify two independent appliances.
            for identity, other in tuple(records.items()):
                if identity != record.device_id and other.host == record.host:
                    records[identity] = replace(
                        other,
                        conflict=True,
                        alternatives=tuple(
                            dict.fromkeys((*other.alternatives, record.device_id))
                        )[:8],
                    )
                    record_for_scan = replace(
                        record_for_scan,
                        conflict=True,
                        alternatives=tuple(
                            dict.fromkeys((*record_for_scan.alternatives, identity))
                        )[:8],
                    )
            if len(records) < 256 or record.device_id in records:
                records[record.device_id] = record_for_scan
                activity.devices = len(records)
            else:
                self.issue("catalog_limit")

    async def _open(self) -> None:
        self._issues = []
        adapters = await async_get_adapters(self.hass)
        interfaces: dict[str, IPv4Interface] = {}
        for adapter in adapters:
            if adapter["enabled"]:
                for info in adapter["ipv4"]:
                    interface = IPv4Interface(
                        f"{info['address']}/{info['network_prefix']}"
                    )
                    if (
                        interface.network.prefixlen < 31
                        and not interface.ip.is_unspecified
                    ):
                        interfaces[str(interface.ip)] = interface
        self._networks = tuple(interface.network for interface in interfaces.values())
        loop = asyncio.get_running_loop()
        # Scope both receive paths to HA-enabled addresses; do not steal ports.
        for source, interface in interfaces.items():
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Receiver(self),
                    local_addr=(source, DISCOVERY_PORT),
                    family=socket.AF_INET,
                )
                self._transports.append(transport)
            except OSError:
                self.issue("fixed_port_unavailable")
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Receiver(self),
                    local_addr=(source, 0),
                    family=socket.AF_INET,
                    allow_broadcast=True,
                )
            except OSError:
                self.issue("interface_unavailable")
            else:
                self._transports.append(transport)
                self._senders.append(
                    (transport, str(interface.network.broadcast_address))
                )
        if not self._senders:
            self.issue("no_enabled_interfaces")

    def _close(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        self._senders.clear()
        self._networks = ()

    async def _stop_sender(self) -> None:
        """Settle the previous run before another can open the shared sockets."""
        releasing = self._sender_task is not None or bool(self._transports)
        try:
            if self._sender_task is not None:
                self._sender_task.cancel()
                await asyncio.gather(self._sender_task, return_exceptions=True)
        finally:
            self._sender_task = None
            self._close()
            if releasing:
                # Transport.close queues UDP handle release; settle it before rebinding.
                await asyncio.sleep(0)

    async def _send_loop(self) -> None:
        """Submit one shared round; joins never alter the next scheduled round."""
        # Admit registrations already waiting on initial socket setup first.
        await asyncio.sleep(0)
        while True:
            if self.hass.state in (
                CoreState.stopping, CoreState.final_write, CoreState.stopped
            ):
                return
            activities = [
                search.activity for search in self._searches
                if not search.activity.stop.is_set()
            ]
            if not activities:
                return
            for activity in activities:
                activity.rounds += 1
            for transport, broadcast in self._senders:
                try:
                    transport.sendto(QUERY, (broadcast, DISCOVERY_PORT))
                except OSError:
                    self.issue("send_failed")
                else:
                    for activity in activities:
                        activity.sent += 1
                        activity.event("sent", f"{broadcast}:{DISCOVERY_PORT}")
            await asyncio.sleep(20)

    @callback
    def _sender_finished(self, task: asyncio.Task[None]) -> None:
        """End participants even when cancellation prevented the first coroutine step."""
        if task is not self._sender_task:
            return
        active = [
            search.activity for search in self._searches
            if not search.activity.stop.is_set()
        ]
        if active:
            shutting_down = self.hass.state in (
                CoreState.stopping, CoreState.final_write, CoreState.stopped
            )
            if not shutting_down:
                self.issue("scan_failed")
            for activity in active:
                activity.finish("shutdown" if shutting_down else "failed")
                activity.stop.set()
        self._close()

    async def scan(self, *, activity: ScanActivity) -> ScanResult:
        """Join shared sending until this interval ends, then return only its records."""
        records: dict[str, DiscoveryRecord] = {}
        activity.event("listening")
        async with self._lock:
            if activity.stop.is_set():
                activity.finish("stopped")
                return ScanResult((), tuple(activity.issues))
            if self.hass.state in (
                CoreState.stopping, CoreState.final_write, CoreState.stopped
            ):
                activity.finish("shutdown")
                activity.stop.set()
                return ScanResult((), tuple(activity.issues))
            if not any(not item.activity.stop.is_set() for item in self._searches):
                await self._stop_sender()
                try:
                    await self._open()
                except BaseException:
                    self._close()
                    raise
            # Opening adapters can suspend while the owning interval is stopped.
            if activity.stop.is_set() or self.hass.state in (
                CoreState.stopping, CoreState.final_write, CoreState.stopped
            ):
                if not activity.stop.is_set():
                    activity.finish("shutdown")
                    activity.stop.set()
                if not any(not item.activity.stop.is_set() for item in self._searches):
                    await self._stop_sender()
                return ScanResult((), tuple(activity.issues))
            search = _Search(records, activity)
            self._searches.append(search)
            for issue in self._issues:
                activity.issues.append(issue)
                activity.event("issue", issue)
            if not self._senders:
                activity.finish("failed")
                activity.stop.set()
            elif self._sender_task is None:
                self._sender_task = self.hass.async_create_background_task(
                    self._send_loop(), f"{DOMAIN} shared discovery sending", eager_start=False
                )
                self._sender_task.add_done_callback(self._sender_finished)
        try:
            await activity.stop.wait()
            activity.finish("stopped")
            return ScanResult(tuple(records.values()), tuple(activity.issues))
        finally:
            self._searches.remove(search)
            async with self._lock:
                if not any(not item.activity.stop.is_set() for item in self._searches):
                    await self._stop_sender()


@singleton(f"{DOMAIN}_local_discovery")
def async_get_collector(hass: HomeAssistant) -> DiscoveryCollector:
    """Return the shared onboarding-only collector."""
    return DiscoveryCollector(hass)
