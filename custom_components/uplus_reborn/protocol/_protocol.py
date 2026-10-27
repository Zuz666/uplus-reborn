"""Plaintext UWT protocol used by the HWD120-B1558U local interface."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from time import monotonic
from typing import Final, Literal

from ._capabilities import Drum, ordinary_stain_choices
from ._commands import BuilderContext, ProgramIntent, encode_start, validate_intent

_LOGGER = logging.getLogger(__name__)
_SESSION_IDS = count(1)
_ERROR_CONTEXT_FIELDS: Final = frozenset(
    {
        "inner_offset",
        "inner_declared_length",
        "inner_received_length",
        "envelope_declared_length",
        "envelope_received_length",
        "state_received_length",
        "state_minimum_length",
    }
)

_UWT_REQUEST: Final = 10004
_UWT_RESPONSE: Final = 10005
_HEARTBEAT_REQUEST: Final = 24050
_HEARTBEAT_RESPONSE: Final = 24051
_MAX_ENVELOPE_PAYLOAD: Final = 4096
_MAX_INNER_SIZE: Final = 0xF1
_FRAME_CONTROL: Final = 0x01
_FRAME_STATUS: Final = 0x02
_FRAME_REPORT: Final = 0x06
_QUERY_STATE: Final = 0x4D01
_STATE_REPORT: Final = 0x6D01
_CODE_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FRAME_ACK: Final = 0x03
type DrumAction = Literal["pause", "resume", "cancel", "reset"]
type ApplianceOption = Literal["power", "buzzer", "child_lock"]

# Exact target profile 205003/4 and 20500u/v, not motion-sensor semantics.
_CYCLE_PHASES: Final = frozenset((*range(1, 18), 20))
_FINISHED_PHASES: Final = frozenset((0x12, 0x13))
_DRUM_COMMANDS: Final = {
    "upper": {"pause": 0x4D05, "resume": 0x4D04, "cancel": 0x4D13, "reset": 0x4D13},
    "lower": {"pause": 0x4D85, "resume": 0x4D84, "cancel": 0x4D93, "reset": 0x4D93},
}
_OPTION_COMMANDS: Final = {
    "power": (0x4D03, 0x4D02),
    "buzzer": (0x4D2F, 0x4D2E),
    "child_lock": (0x4D07, 0x4D06),
}
_CONTROL_INIT_REQUEST: Final = 0x73
_CONTROL_INIT_RESPONSE: Final = 0x74
_RESPONSE_KINDS: Final = {
    _FRAME_STATUS: "status",
    _FRAME_REPORT: "report",
    _FRAME_ACK: "ack",
    _CONTROL_INIT_RESPONSE: "control_init",
}
_HEARTBEAT_INTERVAL: Final = 5.0
# A missing/unknown command response may be resolved by a correlated state query.
_CONFIRMATION_QUERY_DELAY: Final = 0.5
_UPPER_PROGRAM_NAMES: Final = {
    0x00: "none",
    0x07: "kids_wear",
    0x09: "allergy_care",
    0x0D: "drum_clean",
    0x0E: "underwear",
    0x0F: "quick_wash",
    0x13: "delicate_care",
    0x14: "baby_care",
    0x15: "casual",
    0x18: "colored_laundry",
    0x1A: "towels",
    0x1B: "rinse_spin",
    0x1C: "spin",
}
_LOWER_PROGRAM_NAMES: Final = {
    0x00: "none",
    0x01: "cotton",
    0x02: "synthetic",
    0x03: "down",
    0x04: "curtain",
    0x05: "mix",
    0x06: "shirts",
    0x09: "hygiene",
    0x0A: "wool",
    0x0B: "jeans",
    0x0C: "refresh",
    0x0D: "drum_clean",
    0x0F: "quick_wash",
    0x11: "bulky",
    0x12: "sports_wear",
    0x13: "delicate_care",
    0x1B: "rinse_spin",
    0x1C: "spin",
    0x1D: "dry",
    0x1F: "bedding",
    0x20: "outdoor_care",
}
_PHASE_NAMES: Final = {
    0x00: "standby",
    0x01: "weighing",
    0x02: "washing",
    0x03: "washing",
    0x04: "washing",
    0x05: "washing",
    0x06: "washing",
    0x07: "spinning",
    0x08: "spinning",
    0x09: "spinning",
    0x0A: "spinning",
    0x0B: "spinning",
    0x0C: "drying",
    0x0D: "cooling",
    0x0E: "intermittent_rotation",
    0x0F: "delayed_start",
    0x10: "intermittent_rotation",
    0x11: "drying",
    0x12: "drying_finished",
    0x13: "completed",
    0x14: "intermittent_rotation",
}
UPPER_PROGRAM_OPTIONS: Final = tuple(dict.fromkeys(_UPPER_PROGRAM_NAMES.values()))
LOWER_PROGRAM_OPTIONS: Final = tuple(dict.fromkeys(_LOWER_PROGRAM_NAMES.values()))
PHASE_OPTIONS: Final = tuple(dict.fromkeys(_PHASE_NAMES.values()))
UNKNOWN_VALUE: Final = "unsupported"


class ProtocolError(Exception):
    """Base error for the local protocol interface."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        context: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        # Only bounded structural integers may reach exception-derived diagnostics.
        self.context = tuple(
            (key, value)
            for key, value in context[:8]
            if key in _ERROR_CONTEXT_FIELDS and type(value) is int and value >= 0
        )


class CannotConnect(ProtocolError):
    """The appliance did not yield an authoritative state snapshot."""


class InvalidFrame(ProtocolError):
    """A received or supplied frame violates the observed wire contract."""


class StateChanged(ProtocolError):
    """The appliance changed after the caller's expected snapshot."""


class UnsafeState(ProtocolError):
    """A write was refused because verified safety preconditions were absent."""


class OutcomeUnknown(ProtocolError):
    """A one-shot write was sent, but its resulting state was not proven."""


def classify_error(error: BaseException) -> str:
    """Return an allowlisted cause, never exception text or identifying data."""
    if isinstance(error, ProtocolError) and error.reason in {
        "power_off",
        "remote_control_off",
        "local_lock",
        "drum_not_standby",
        "invalid_intent",
        "invalid_action",
        "invalid_option",
        "child_lock",
        "not_active",
        "not_paused",
        "not_finished",
        "unsupported_phase",
        "controls_disabled",
        "program_not_staged",
        "no_snapshot",
        "partial_envelope",
        "invalid_escape",
        "checksum_mismatch",
        "truncated_inner",
        "unsupported_flags",
        "invalid_inner_size",
        "invalid_state_length",
    }:
        return error.reason
    if isinstance(error, OutcomeUnknown):
        return "outcome_unknown"
    if isinstance(error, StateChanged):
        return "state_changed"
    if isinstance(error, UnsafeState):
        return "unsafe_state"
    if isinstance(error, InvalidFrame):
        return "invalid_frame"
    if isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError):
        return "timeout"
    if isinstance(error, OSError) or isinstance(error.__cause__, OSError):
        return "connection_error"
    if isinstance(error, CannotConnect):
        return "connection_closed"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "protocol_error"


@dataclass(frozen=True, slots=True)
class ProgramSettingsReport:
    """Reported effective settings and stage counters, not a local draft.

    Wash/spin times can decrease; the stain coordinate can persist after panel
    deselection and does not prove an active treatment. Speed is absent because
    captured nonzero selections report zero at the profile speed coordinates.
    Numeric end-delay has no established report field. Never fill gaps from
    requested values or interpret counters as original configured durations.
    """

    temperature_c: int
    wash_minutes_encoded: int
    rinses: int
    spin_minutes: int
    option_bits: int
    smart: bool
    drying_value: int | None
    stain_value: int


@dataclass(frozen=True, slots=True)
class DrumSnapshot:
    """Immutable state for one drum.

    Remaining time is absent in standby; the appliance may retain a
    non-display value in those raw words.
    """

    program_value: int
    program_code: str | None
    program: str
    phase_value: int
    phase_code: str | None
    phase: str
    door_locked: bool
    remaining_minutes: int | None
    cycle_count: int
    # Profile word 3/26 bit 7: Start/Pause command state, not physical motion.
    cycle_active: bool = False
    # Profile 20501a/i, word 2/25 bit 2: reservation selected.
    delay_active: bool = False
    builder_context: BuilderContext = field(default_factory=BuilderContext)
    reported_settings: ProgramSettingsReport | None = None


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Authoritative immutable state decoded from one checksum-valid report."""

    observed_at: datetime
    power: bool
    remote_control: bool
    local_lock: bool
    upper: DrumSnapshot
    lower: DrumSnapshot
    current_water_raw: int = 0
    total_water_raw: int = 0
    current_electricity_raw: int = 0
    total_electricity_raw: int = 0
    # Exact profile 20500l/m: word 2 bit 8 is inverted (0 means enabled).
    buzzer_enabled: bool = True
    # Exact profile 205005/6: word 2 bit 3, distinct from local_lock.
    child_lock: bool = False


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Fresh action proof with explicit limits on configured-setting proof."""

    before: DeviceSnapshot
    after: DeviceSnapshot
    unconfirmed_settings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Envelope:
    message_id: int
    opaque_word: int
    opaque_identifier: bytes
    device_id: bytes
    sequence: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class _InnerFrame:
    flags: int
    reserved: bytes
    frame_type: int
    payload: bytes


def _response_kind(frame: _InnerFrame) -> str:
    """Describe a frame type without guessing unsupported response semantics."""
    return _RESPONSE_KINDS.get(frame.frame_type, "unexpected")


class _EnvelopeDecoder:
    """Incrementally decode UWT envelopes from an arbitrary TCP byte stream."""

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> tuple[_Envelope, ...]:
        self._buffer.extend(data)
        decoded: list[_Envelope] = []

        while len(self._buffer) >= 4:
            message_id = int.from_bytes(self._buffer[:4], "big")
            if message_id in (_UWT_REQUEST, _UWT_RESPONSE):
                header_size = 80
                length_offset = 76
            elif message_id in (_HEARTBEAT_REQUEST, _HEARTBEAT_RESPONSE):
                header_size = 16
                length_offset = 12
            else:
                raise InvalidFrame(f"Unsupported UWT message ID {message_id}")

            if len(self._buffer) < header_size:
                break

            payload_length = int.from_bytes(
                self._buffer[length_offset : length_offset + 4], "big"
            )
            if payload_length > _MAX_ENVELOPE_PAYLOAD:
                raise InvalidFrame(
                    f"UWT payload length {payload_length} exceeds "
                    f"{_MAX_ENVELOPE_PAYLOAD}",
                    context=(("envelope_declared_length", payload_length),),
                )

            envelope_size = header_size + payload_length
            if len(self._buffer) < envelope_size:
                break

            packet = bytes(self._buffer[:envelope_size])
            del self._buffer[:envelope_size]

            if header_size == 80:
                opaque_word = int.from_bytes(packet[4:8], "big")
                opaque_identifier = packet[8:40].split(b"\0", 1)[0]
                device_id = packet[40:72].split(b"\0", 1)[0]
                sequence = int.from_bytes(packet[72:76], "big")
            else:
                opaque_word = int.from_bytes(packet[4:8], "big")
                opaque_identifier = b""
                device_id = b""
                sequence = int.from_bytes(packet[8:12], "big")

            decoded.append(
                _Envelope(
                    message_id=message_id,
                    opaque_word=opaque_word,
                    opaque_identifier=opaque_identifier,
                    device_id=device_id,
                    sequence=sequence,
                    payload=packet[header_size:],
                )
            )

        return tuple(decoded)


def _read_escaped_octet(raw: bytes, offset: int) -> tuple[int, int, bool]:
    if offset >= len(raw):
        raise InvalidFrame(
            "Truncated inner frame",
            reason="truncated_inner",
            context=(("inner_offset", offset), ("inner_received_length", len(raw))),
        )

    value = raw[offset]
    offset += 1
    if value != 0xFF:
        return value, offset, False

    if offset >= len(raw) or raw[offset] != 0x55:
        raise InvalidFrame(
            "Unescaped 0xFF in inner frame body",
            reason="invalid_escape",
            context=(("inner_offset", offset - 1), ("inner_received_length", len(raw))),
        )
    return value, offset + 1, True


def _decode_inner_frame(raw: bytes) -> tuple[_InnerFrame, int]:
    if not raw.startswith(b"\xff\xff"):
        raise InvalidFrame("Inner frame does not start with FF FF")

    offset = 2
    body = bytearray()
    checksum_sum = 0

    declared_size, offset, escaped = _read_escaped_octet(raw, offset)
    if declared_size < 8 or declared_size > _MAX_INNER_SIZE:
        raise InvalidFrame(
            f"Invalid inner frame size {declared_size}",
            reason="invalid_inner_size",
            context=(
                ("inner_declared_length", declared_size),
                ("inner_received_length", len(raw)),
            ),
        )
    body.append(declared_size)
    checksum_sum = declared_size + (0x55 if escaped else 0)

    while len(body) < declared_size:
        value, offset, escaped = _read_escaped_octet(raw, offset)
        body.append(value)
        checksum_sum += value + (0x55 if escaped else 0)

    checksum_offset = offset
    # Device reports use a literal terminal FF; the APK encoder uses FF 55.
    # Only the checksum boundary permits both forms, never the frame body.
    if offset == len(raw) - 1:
        checksum = raw[offset]
        offset += 1
    else:
        checksum, offset, _ = _read_escaped_octet(raw, offset)
    if checksum != checksum_sum & 0xFF:
        raise InvalidFrame(
            f"Inner checksum mismatch: got {checksum:02x}, "
            f"expected {checksum_sum & 0xFF:02x}",
            reason="checksum_mismatch",
            context=(
                ("inner_offset", checksum_offset),
                ("inner_declared_length", declared_size),
                ("inner_received_length", len(raw)),
            ),
        )

    flags = body[1]
    if flags & 0x40:
        raise InvalidFrame(
            "CRC-bearing inner frames are not verified for this model",
            reason="unsupported_flags",
        )
    if flags & ~0x40:
        raise InvalidFrame(
            f"Unsupported inner frame flags {flags:02x}", reason="unsupported_flags"
        )

    return (
        _InnerFrame(
            flags=flags,
            reserved=bytes(body[2:7]),
            frame_type=body[7],
            payload=bytes(body[8:]),
        ),
        offset,
    )


def _escape_octets(data: bytes) -> bytes:
    encoded = bytearray()
    for value in data:
        encoded.append(value)
        if value == 0xFF:
            encoded.append(0x55)
    return bytes(encoded)


def _build_inner_frame(frame_type: int, payload: bytes) -> bytes:
    declared_size = 8 + len(payload)
    if declared_size > _MAX_INNER_SIZE:
        raise ValueError(f"Inner frame size exceeds {_MAX_INNER_SIZE}")

    body = bytes((declared_size, 0, 0, 0, 0, 0, 0, frame_type)) + payload
    encoded_body = _escape_octets(body)
    checksum = sum(encoded_body) & 0xFF
    return b"\xff\xff" + encoded_body + _escape_octets(bytes((checksum,)))


def _build_request(device_id: str, sequence: int, inner_frame: bytes) -> bytes:
    encoded_device_id = device_id.encode("ascii")
    if (
        not encoded_device_id
        or len(encoded_device_id) > 31
        or b"\0" in encoded_device_id
    ):
        raise ValueError("device_id must contain 1-31 non-NUL ASCII bytes")
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must fit an unsigned 32-bit integer")

    return (
        struct.pack(
            ">II32s32sII",
            _UWT_REQUEST,
            0,
            b"",
            encoded_device_id,
            sequence,
            len(inner_frame),
        )
        + inner_frame
    )


def _build_heartbeat_request(device_id: str, counter: int) -> bytes:
    encoded_device_id = device_id.encode("ascii")
    if (
        not encoded_device_id
        or len(encoded_device_id) > 16
        or b"\0" in encoded_device_id
    ):
        raise ValueError("heartbeat device_id must contain 1-16 non-NUL ASCII bytes")
    if not 0 <= counter <= 0xFFFFFFFF:
        raise ValueError("heartbeat counter must fit an unsigned 32-bit integer")

    payload = struct.pack(">32s16s", b"", encoded_device_id)
    return (
        struct.pack(
            ">IIII",
            _HEARTBEAT_REQUEST,
            0,
            counter,
            len(payload),
        )
        + payload
    )


def _state_words(frame: _InnerFrame) -> tuple[int, ...]:
    if frame.frame_type not in (_FRAME_STATUS, _FRAME_REPORT):
        raise InvalidFrame(f"Frame type {frame.frame_type:02x} is not state")
    if len(frame.payload) < 82 or len(frame.payload) % 2:
        raise InvalidFrame(
            f"State payload must contain at least 82 even bytes, got {len(frame.payload)}",
            reason="invalid_state_length",
            context=(
                ("state_minimum_length", 82),
                ("state_received_length", len(frame.payload)),
            ),
        )

    words = tuple(
        int.from_bytes(frame.payload[offset : offset + 2], "big")
        for offset in range(0, len(frame.payload), 2)
    )
    if words[0] != _STATE_REPORT:
        raise InvalidFrame(f"Unexpected state command {words[0]:04x}")
    return words


def _field(words: tuple[int, ...], word: int, bit: int, width: int) -> int:
    if word < 0 or bit < 0 or not 1 <= width <= 32:
        raise ValueError("Invalid EPP field coordinate")

    word_count = (bit + width + 15) // 16
    if word + word_count > len(words):
        raise InvalidFrame("EPP field extends beyond the state payload")

    value = 0
    for item in words[word : word + word_count]:
        value = value << 16 | item
    return value >> bit & ((1 << width) - 1)


def _profile_code(value: int) -> str | None:
    if value >= len(_CODE_ALPHABET):
        return None
    return f"30500{_CODE_ALPHABET[value]}"


def _program_name(value: int, names: dict[int, str]) -> str:
    return names.get(value, UNKNOWN_VALUE)


def _phase_name(value: int) -> str:
    return _PHASE_NAMES.get(value, UNKNOWN_VALUE)


def _decode_state(words: tuple[int, ...], observed_at: datetime) -> DeviceSnapshot:
    """Project words validated by _state_words into an authoritative snapshot."""
    upper_hours = _field(words, 4, 0, 8)
    upper_minutes = _field(words, 5, 8, 8)
    lower_hours = _field(words, 27, 0, 8)
    lower_minutes = _field(words, 28, 8, 8)
    upper_program = _field(words, 1, 8, 8)
    lower_program = _field(words, 24, 8, 8)
    upper_phase = _field(words, 3, 8, 8)
    lower_phase = _field(words, 26, 8, 8)

    return DeviceSnapshot(
        observed_at=observed_at,
        power=bool(_field(words, 3, 0, 1)),
        remote_control=bool(_field(words, 3, 1, 1)),
        local_lock=bool(_field(words, 3, 3, 1)),
        upper=DrumSnapshot(
            program_value=upper_program,
            program_code=_profile_code(upper_program),
            program=_program_name(upper_program, _UPPER_PROGRAM_NAMES),
            phase_value=upper_phase,
            phase_code=_profile_code(upper_phase),
            phase=_phase_name(upper_phase),
            door_locked=bool(_field(words, 3, 5, 1)),
            remaining_minutes=(
                upper_hours * 60 + upper_minutes if upper_phase != 0 else None
            ),
            cycle_count=_field(words, 7, 0, 16),
            cycle_active=bool(_field(words, 3, 7, 1)),
            delay_active=bool(_field(words, 2, 2, 1)),
            builder_context=BuilderContext(
                load_level=_field(words, 15, 8, 8),
                wash_water_level=_field(words, 23, 0, 16),
                scrubbing_speed=_field(words, 21, 8, 8),
                washing_rotation_time=_field(words, 22, 8, 8),
                washing_stop_time=_field(words, 22, 0, 8),
            ),
            reported_settings=ProgramSettingsReport(
                temperature_c=_field(words, 16, 8, 8),
                wash_minutes_encoded=_field(words, 16, 0, 8),
                rinses=_field(words, 14, 0, 8),
                spin_minutes=_field(words, 15, 0, 8),
                option_bits=_field(words, 17, 0, 16) & 0x6F,
                smart=bool(_field(words, 2, 9, 1)),
                drying_value=None,
                stain_value=_field(words, 18, 8, 8),
            ),
        ),
        lower=DrumSnapshot(
            program_value=lower_program,
            program_code=_profile_code(lower_program),
            program=_program_name(lower_program, _LOWER_PROGRAM_NAMES),
            phase_value=lower_phase,
            phase_code=_profile_code(lower_phase),
            phase=_phase_name(lower_phase),
            door_locked=bool(_field(words, 26, 5, 1)),
            remaining_minutes=(
                lower_hours * 60 + lower_minutes if lower_phase != 0 else None
            ),
            cycle_count=_field(words, 30, 0, 16),
            cycle_active=bool(_field(words, 26, 7, 1)),
            delay_active=bool(_field(words, 25, 2, 1)),
            builder_context=BuilderContext(
                load_level=_field(words, 32, 8, 8),
                wash_water_level=_field(words, 40, 0, 16),
                scrubbing_speed=_field(words, 38, 8, 8),
                washing_rotation_time=_field(words, 39, 8, 8),
                washing_stop_time=_field(words, 39, 0, 8),
                drying_value=_field(words, 24, 0, 8),
            ),
            reported_settings=ProgramSettingsReport(
                temperature_c=_field(words, 33, 8, 8),
                wash_minutes_encoded=_field(words, 33, 0, 8),
                rinses=_field(words, 31, 0, 8),
                spin_minutes=_field(words, 32, 0, 8),
                option_bits=_field(words, 34, 0, 16) & 0x6F,
                smart=bool(_field(words, 25, 9, 1)),
                drying_value=_field(words, 24, 0, 8),
                stain_value=_field(words, 35, 8, 8),
            ),
        ),
        current_water_raw=_field(words, 8, 0, 16),
        total_water_raw=_field(words, 9, 0, 32),
        current_electricity_raw=_field(words, 11, 0, 16),
        total_electricity_raw=_field(words, 12, 0, 32),
        buzzer_enabled=not bool(_field(words, 2, 8, 1)),
        child_lock=bool(_field(words, 2, 3, 1)),
    )


@dataclass(frozen=True, slots=True)
class _StateResult:
    snapshot: DeviceSnapshot
    words: tuple[int, ...]
    envelope: _Envelope
    frame: _InnerFrame


def _state_result(envelope: _Envelope, frame: _InnerFrame) -> _StateResult | None:
    if frame.frame_type not in (_FRAME_STATUS, _FRAME_REPORT):
        return None
    if len(frame.payload) < 2:
        return None
    if int.from_bytes(frame.payload[:2], "big") != _STATE_REPORT:
        return None
    observed_at = datetime.now(UTC)
    words = _state_words(frame)
    return _StateResult(
        snapshot=_decode_state(words, observed_at),
        words=words,
        envelope=envelope,
        frame=frame,
    )


def _drum_state(snapshot: DeviceSnapshot, drum: Drum) -> DrumSnapshot:
    if drum == "upper":
        return snapshot.upper
    if drum == "lower":
        return snapshot.lower
    raise UnsafeState("Unknown target drum", reason="invalid_intent")


def _require_live(allow_live: bool) -> None:
    if allow_live is not True:
        raise UnsafeState("Live controls are disabled", reason="controls_disabled")


def _validate_remote(
    snapshot: DeviceSnapshot,
    *,
    require_power: bool = True,
    unlock_child: bool = False,
) -> None:
    if require_power and not snapshot.power:
        raise UnsafeState("Control requires appliance power", reason="power_off")
    if not snapshot.remote_control:
        raise UnsafeState(
            "Control requires physical Remote Control", reason="remote_control_off"
        )
    if snapshot.local_lock and not (unlock_child and snapshot.child_lock):
        raise UnsafeState("Control is blocked by the local lock", reason="local_lock")


def _validate_cycle_control(snapshot: DeviceSnapshot) -> None:
    _validate_remote(snapshot)
    if snapshot.child_lock:
        raise UnsafeState("Control is blocked by child lock", reason="child_lock")


def validate_program_start(snapshot: DeviceSnapshot, intent: ProgramIntent) -> None:
    """Validate an exact-target new program without constraining the other drum.

    No decoded bit establishes physical door closure or absence of an alarm.
    Appliance interlocks remain authoritative; confirmation is never optimistic.
    """
    try:
        validate_intent(intent)
    except (TypeError, ValueError) as error:
        raise UnsafeState("Invalid program intent", reason="invalid_intent") from error
    _validate_cycle_control(snapshot)
    target = _drum_state(snapshot, intent.drum)
    if target.phase_value != 0 or target.cycle_active:
        raise UnsafeState(
            "New program requires the target drum in standby", reason="drum_not_standby"
        )


def validate_drum_control(
    snapshot: DeviceSnapshot, drum: Drum, action: DrumAction
) -> None:
    """Validate source Start/Pause state and explicit operational/finished phases."""
    _validate_cycle_control(snapshot)
    target = _drum_state(snapshot, drum)
    if action not in ("pause", "resume", "cancel", "reset"):
        raise UnsafeState("Unknown drum action", reason="invalid_action")
    if action == "reset":
        if target.phase_value not in _FINISHED_PHASES:
            raise UnsafeState("Reset requires a finished drum", reason="not_finished")
        return
    if target.phase_value not in _CYCLE_PHASES:
        raise UnsafeState(
            "Action requires a known cycle phase", reason="unsupported_phase"
        )
    if action == "pause" and not target.cycle_active:
        raise UnsafeState("Pause requires an active cycle", reason="not_active")
    if action == "resume" and target.cycle_active:
        raise UnsafeState("Resume requires a paused cycle", reason="not_paused")


def _validate_appliance_option(
    snapshot: DeviceSnapshot, option: ApplianceOption, enabled: bool
) -> None:
    if option not in _OPTION_COMMANDS or type(enabled) is not bool:
        raise UnsafeState("Unknown appliance option or value", reason="invalid_option")
    # The child-lock setting can also assert the local-lock flag.
    _validate_remote(
        snapshot,
        require_power=not (option == "power" and enabled),
        unlock_child=option == "child_lock" and not enabled,
    )
    if snapshot.child_lock and option != "child_lock":
        raise UnsafeState("Control is blocked by child lock", reason="child_lock")


def _control_revision(
    snapshot: DeviceSnapshot,
    drum: Drum | None,
    *,
    new_program: bool = False,
    option: ApplianceOption | None = None,
) -> tuple[object, ...]:
    """Compare only dispatch-relevant state, never countdowns or consumption."""
    shared = (
        snapshot.power,
        snapshot.remote_control,
        snapshot.local_lock,
        snapshot.child_lock,
    )
    if drum is None:
        return (*shared, _option_value(snapshot, option))
    target = _drum_state(snapshot, drum)
    if new_program:
        return (
            *shared,
            target.phase_value,
            target.cycle_active,
            snapshot.buzzer_enabled,
            target.builder_context,
        )
    return (
        *shared,
        target.program_value,
        target.phase_value,
        target.cycle_active,
        target.delay_active,
    )


def _require_expected_state(
    expected: DeviceSnapshot | None,
    current: DeviceSnapshot,
    revision: Callable[[DeviceSnapshot], tuple[object, ...]],
) -> None:
    if expected is not None and revision(expected) != revision(current):
        raise StateChanged("Relevant appliance state changed; refresh before control")


def _option_value(snapshot: DeviceSnapshot, option: ApplianceOption | None) -> bool:
    if option == "power":
        return snapshot.power
    if option == "buzzer":
        return snapshot.buzzer_enabled
    return snapshot.child_lock


def _is_program_started(
    snapshot: DeviceSnapshot, intent: ProgramIntent, dispatched_payload: bytes
) -> bool:
    target = _drum_state(snapshot, intent.drum)
    if (
        not snapshot.power
        or target.program != intent.program
        or not target.cycle_active
    ):
        return False
    settings = target.reported_settings
    if settings is None:
        return False
    group = struct.unpack(">12H", dispatched_payload)
    # Dry reuses the command wash-time coordinate for drying duration, but its
    # report has no main wash. The selected drying byte is checked separately.
    wash_minutes = 0 if intent.program == "dry" else group[4] & 0xFF
    if (
        settings.temperature_c != group[4] >> 8
        or settings.wash_minutes_encoded != wash_minutes
        or settings.rinses != group[6] >> 8
        or settings.spin_minutes != group[6] & 0xFF
        or settings.option_bits != group[7] & 0x6F
        or settings.smart != bool(group[2] & 0x02)
        or snapshot.buzzer_enabled != (not bool(group[2] & 0x01))
        or snapshot.child_lock != bool(group[3] & 0x0400)
        or (intent.drum == "lower" and settings.drying_value != group[2] >> 8)
        or (
            not intent.smart
            and ordinary_stain_choices(intent.drum, intent.program)
            and settings.stain_value != group[8] >> 8
        )
    ):
        return False
    if intent.delay_half_hour_step:
        return target.phase_value == 0x0F and target.delay_active
    if target.delay_active or target.phase_value == 0x0F:
        return False
    # Program identity and settings establish the requested operation. Phase
    # labels are not program-exclusive: Dry can start in shared phase 05.
    return target.phase_value in _CYCLE_PHASES


def _is_drum_controlled(
    snapshot: DeviceSnapshot, before: DeviceSnapshot, drum: Drum, action: DrumAction
) -> bool:
    target = _drum_state(snapshot, drum)
    if not snapshot.power:
        return False
    if action in ("cancel", "reset"):
        # Standby does not promise draining, physical unlock or an empty machine.
        return target.phase_value == 0 and not target.cycle_active
    previous = _drum_state(before, drum)
    return (
        target.program_value == previous.program_value
        and target.phase_value in _CYCLE_PHASES
        and target.cycle_active == (action == "resume")
        and (target.phase_value != 0x0F or target.delay_active)
    )


@dataclass(slots=True)
class _LocalSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    device_id: str
    correlation: int
    started_at: float
    decoder: _EnvelopeDecoder = field(default_factory=_EnvelopeDecoder)
    pending: deque[_Envelope] = field(default_factory=deque)
    state_backlog: deque[_StateResult] = field(default_factory=deque)
    control_name: str | None = None
    next_sequence: int = 0
    heartbeat_due: float | None = None
    request_kind: str = "state_query"
    request_sequence: int | None = None
    request_started_at: float = 0

    def log_event(self, event: str, **context: object) -> None:
        """Log only caller-selected scalar context, never a frame or identity."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "session=%s kind=%s sequence=%s elapsed=%.3f event=%s context=%s",
                self.correlation,
                self.request_kind,
                self.request_sequence,
                monotonic() - self.started_at,
                event,
                context,
            )

    def begin_request(self, kind: str, sequence: int) -> None:
        self.request_kind = kind
        self.request_sequence = sequence
        self.request_started_at = monotonic()
        self.log_event("dispatch")

    def _allocate_sequence(self) -> int:
        sequence = self.next_sequence
        self.next_sequence = (self.next_sequence + 1) & 0xFFFFFFFF
        return sequence

    async def _send(self, sequence: int, frame_type: int, payload: bytes) -> None:
        request = _build_request(
            self.device_id,
            sequence=sequence,
            inner_frame=_build_inner_frame(frame_type, payload),
        )
        self.writer.write(request)
        await self.writer.drain()

    async def _send_state_query(self) -> int:
        """Dispatch one query; its caller owns all incoming proof channels."""
        sequence = self._allocate_sequence()
        self.begin_request("state_query", sequence)
        await self._send(
            sequence=sequence,
            frame_type=_FRAME_CONTROL,
            payload=_QUERY_STATE.to_bytes(2, "big"),
        )
        return sequence

    async def query_state(self) -> _StateResult:
        sequence = await self._send_state_query()
        while True:
            envelope, frame = await self.receive_inner()
            if result := _state_result(envelope, frame):
                if envelope.sequence == sequence:
                    self.log_event("correlated", response_kind=_response_kind(frame))
                    return result
                self.log_event("uncorrelated", response_sequence=envelope.sequence)
                self.state_backlog.append(result)

    async def initialize_controls(
        self,
        expected: DeviceSnapshot,
        check: Callable[[DeviceSnapshot], None] | None = None,
    ) -> DeviceSnapshot:
        sequence = self._allocate_sequence()
        self.begin_request("control_init", sequence)
        await self._send(
            sequence=sequence,
            frame_type=_CONTROL_INIT_REQUEST,
            payload=b"",
        )
        current = expected
        while True:
            envelope, frame = await self.receive_inner()
            if result := _state_result(envelope, frame):
                if check is not None:
                    check(result.snapshot)
                current = result.snapshot
                self.state_backlog.append(result)
            if (
                envelope.sequence == sequence
                and frame.frame_type == _CONTROL_INIT_RESPONSE
            ):
                # Reports already received before dispatch cannot prove its result.
                while self.pending:
                    pending = self.pending.popleft()
                    if pending.message_id != _UWT_RESPONSE:
                        continue
                    inner, consumed = _decode_inner_frame(pending.payload)
                    if consumed != len(pending.payload):
                        raise InvalidFrame("Trailing bytes after inner frame")
                    if result := _state_result(pending, inner):
                        if check is not None:
                            check(result.snapshot)
                        current = result.snapshot
                        self.state_backlog.append(result)
                self.log_event("correlated", response_kind=_response_kind(frame))
                self.heartbeat_due = (
                    asyncio.get_running_loop().time() + _HEARTBEAT_INTERVAL
                )
                return current

    def drain_state_backlog(self) -> tuple[_StateResult, ...]:
        results = tuple(self.state_backlog)
        self.state_backlog.clear()
        return results

    async def send_control(self, name: str, *, frame_type: int, payload: bytes) -> int:
        if self.control_name is not None:
            raise ProtocolError("A local session may contain only one control write")
        sequence = self._allocate_sequence()
        request = _build_request(
            self.device_id,
            sequence=sequence,
            inner_frame=_build_inner_frame(frame_type, payload),
        )
        self.control_name = name
        self.begin_request(name, sequence)
        self.writer.write(request)
        await self.writer.drain()
        return sequence

    async def confirm_control(
        self, sequence: int, predicate: Callable[[DeviceSnapshot], bool]
    ) -> DeviceSnapshot:
        """Prove requested state, never treating raw 05 as an acknowledgement.

        Normal proof is a matching accepted 02/03 plus an independent report.
        A post-dispatch query can instead establish requested state without that
        response. Pending snapshots schedule further serialized state queries
        within the same deadline. The control write is never resent.
        """
        accepted = False
        observed: DeviceSnapshot | None = None
        query_due = asyncio.get_running_loop().time() + _CONFIRMATION_QUERY_DELAY
        query_sequence: int | None = None
        while True:
            if query_sequence is None:
                try:
                    async with asyncio.timeout_at(query_due):
                        envelope, frame = await self.receive_inner()
                except TimeoutError:
                    query_sequence = await self._send_state_query()
                    continue
            else:
                envelope, frame = await self.receive_inner()
            result = _state_result(envelope, frame)
            if (
                query_sequence is not None
                and envelope.sequence == query_sequence
                and result is not None
            ):
                self.log_event("correlated", response_kind=_response_kind(frame))
                if predicate(result.snapshot):
                    self.log_event("postcondition_observed", proof="independent_query")
                    return result.snapshot
                self.log_event(
                    "postcondition_pending",
                    control=self.control_name,
                    power=result.snapshot.power,
                    buzzer_enabled=result.snapshot.buzzer_enabled,
                    child_lock=result.snapshot.child_lock,
                    upper=result.snapshot.upper,
                    lower=result.snapshot.lower,
                )
                observed = None
                query_sequence = None
                query_due = (
                    asyncio.get_running_loop().time() + _CONFIRMATION_QUERY_DELAY
                )
                continue
            if envelope.sequence == sequence and (
                frame.frame_type == _FRAME_ACK
                or (frame.frame_type == _FRAME_STATUS and result is not None)
            ):
                accepted = True
                self.log_event("correlated", response_kind=_response_kind(frame))
            if (
                result is not None
                and envelope.sequence == 0
                and frame.frame_type == _FRAME_REPORT
            ):
                # Latest evidence supersedes an earlier transient match.
                observed = result.snapshot if predicate(result.snapshot) else None
            if accepted and observed is not None:
                self.log_event("postcondition_observed", proof="accepted_and_report")
                return observed

    async def _send_heartbeat(self) -> None:
        sequence = self._allocate_sequence()
        request = _build_heartbeat_request(self.device_id, sequence)
        self.log_event("heartbeat_dispatch", heartbeat_sequence=sequence)
        self.writer.write(request)
        await self.writer.drain()
        self.heartbeat_due = asyncio.get_running_loop().time() + _HEARTBEAT_INTERVAL

    async def receive_inner(self) -> tuple[_Envelope, _InnerFrame]:
        while True:
            while not self.pending:
                if self.heartbeat_due is None:
                    chunk = await self.reader.read(4096)
                else:
                    heartbeat_delay = max(
                        0.0,
                        self.heartbeat_due - asyncio.get_running_loop().time(),
                    )
                    try:
                        async with asyncio.timeout(heartbeat_delay):
                            chunk = await self.reader.read(4096)
                    except TimeoutError:
                        await self._send_heartbeat()
                        continue
                if not chunk:
                    if self.decoder.buffered_bytes:
                        raise InvalidFrame(
                            "Connection closed with a partial envelope",
                            reason="partial_envelope",
                            context=(
                                (
                                    "envelope_received_length",
                                    self.decoder.buffered_bytes,
                                ),
                            ),
                        )
                    raise CannotConnect(
                        "Connection closed before the required response arrived"
                    )
                self.pending.extend(self.decoder.feed(chunk))

            envelope = self.pending.popleft()
            if envelope.message_id != _UWT_RESPONSE:
                self.log_event(
                    "non_state_envelope", response_sequence=envelope.sequence
                )
                continue
            inner, consumed = _decode_inner_frame(envelope.payload)
            if consumed != len(envelope.payload):
                raise InvalidFrame(
                    "Trailing bytes after inner frame",
                    context=(
                        ("inner_offset", consumed),
                        ("inner_received_length", len(envelope.payload)),
                    ),
                )
            self.log_event(
                "response",
                response_kind=_response_kind(inner),
                response_sequence=envelope.sequence,
                response_type=inner.frame_type,
                request_elapsed=monotonic() - self.request_started_at,
            )
            return envelope, inner


@dataclass(frozen=True, slots=True)
class UwtClient:
    """Short-lived local client for one HWD120-B1558U appliance."""

    host: str
    device_id: str
    port: int = 56800
    timeout: float = 5.0
    control_timeout: float = 15.0

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.control_timeout <= 0:
            raise ValueError("control_timeout must be positive")
        encoded_device_id = self.device_id.encode("ascii")
        if (
            not encoded_device_id
            or len(encoded_device_id) > 16
            or b"\0" in encoded_device_id
        ):
            raise ValueError("device_id must contain 1-16 non-NUL ASCII bytes")

    async def read_state(self) -> DeviceSnapshot:
        """Open one session, send one read-only query, and return fresh state.

        The query is never retried. The connection is closed immediately after
        the first checksum-valid matching-sequence `6d01` status/report frame.
        """

        return (await self._read_state_result()).snapshot

    async def start_program(
        self,
        intent: ProgramIntent,
        *,
        allow_live: bool,
        expected: DeviceSnapshot | None = None,
    ) -> ControlResult:
        """Send one complete grouped new-program intent for the chosen drum.

        Source-derived programs are not a hardware allowlist. Physical interlocks
        and alarms must be respected by the caller/appliance; no state bit here
        claims physical door closure. Retained standby programs do not select
        direct Resume. Shared buzzer/child-lock fields come from fresh state.
        The result names selected speed/end-delay values that reports cannot prove.
        """
        _require_live(allow_live)
        try:
            validate_intent(intent)
        except (TypeError, ValueError) as error:
            raise UnsafeState(
                "Invalid program intent", reason="invalid_intent"
            ) from error
        return await self._execute_control(
            f"{intent.drum} Start",
            expected=expected,
            validate=lambda state: validate_program_start(state, intent),
            revision=lambda state: _control_revision(
                state, intent.drum, new_program=True
            ),
            frame_type=0x60,
            payload=lambda state: encode_start(
                intent,
                buzzer_enabled=state.buzzer_enabled,
                child_lock=state.child_lock,
                context=_drum_state(state, intent.drum).builder_context,
            ),
            postcondition=lambda before, after, payload: _is_program_started(
                after, intent, payload
            ),
            unconfirmed_settings=(
                (("spin_rpm",) if intent.spin_rpm is not None else ())
                + (("delay_half_hour_step",) if intent.delay_half_hour_step else ())
                + (("drying_mode",) if intent.program == "refresh" else ())
            ),
        )

    async def control_drum(
        self,
        drum: Drum,
        action: DrumAction,
        *,
        allow_live: bool,
        expected: DeviceSnapshot | None = None,
    ) -> ControlResult:
        """Pause/resume a cycle or request standby once; Cancel is not Drain."""
        _require_live(allow_live)
        if drum not in _DRUM_COMMANDS or action not in _DRUM_COMMANDS[drum]:
            raise UnsafeState("Unknown drum action", reason="invalid_action")
        return await self._execute_control(
            f"{drum} {action}",
            expected=expected,
            validate=lambda state: validate_drum_control(state, drum, action),
            revision=lambda state: _control_revision(state, drum),
            frame_type=_FRAME_CONTROL,
            payload=lambda state: _DRUM_COMMANDS[drum][action].to_bytes(2, "big"),
            postcondition=lambda before, after, payload: _is_drum_controlled(
                after, before, drum, action
            ),
        )

    async def set_appliance_option(
        self,
        option: ApplianceOption,
        enabled: bool,
        *,
        allow_live: bool,
        expected: DeviceSnapshot | None = None,
    ) -> ControlResult:
        """Set one source-defined shared option; Power cannot wake offline LAN.

        Power-off disconnect alone is unknown, never proof of the requested state.
        """
        _require_live(allow_live)
        if option not in _OPTION_COMMANDS or type(enabled) is not bool:
            raise UnsafeState(
                "Unknown appliance option or value", reason="invalid_option"
            )
        return await self._execute_control(
            f"{option} {'on' if enabled else 'off'}",
            expected=expected,
            validate=lambda state: _validate_appliance_option(state, option, enabled),
            revision=lambda state: _control_revision(state, None, option=option),
            frame_type=_FRAME_CONTROL,
            payload=lambda state: _OPTION_COMMANDS[option][enabled].to_bytes(2, "big"),
            postcondition=lambda before, after, payload: (
                _option_value(after, option) == enabled
            ),
        )

    async def _execute_control(
        self,
        name: str,
        *,
        expected: DeviceSnapshot | None,
        validate: Callable[[DeviceSnapshot], None],
        revision: Callable[[DeviceSnapshot], tuple[object, ...]],
        frame_type: int,
        payload: Callable[[DeviceSnapshot], bytes],
        postcondition: Callable[[DeviceSnapshot, DeviceSnapshot, bytes], bool],
        unconfirmed_settings: tuple[str, ...] = (),
    ) -> ControlResult:
        async with self._session(self.control_timeout) as session:
            before = (await session.query_state()).snapshot
            _require_expected_state(expected, before, revision)
            validate(before)

            def check(current: DeviceSnapshot) -> None:
                _require_expected_state(before, current, revision)
                validate(current)

            before = await session.initialize_controls(before, check)
            validate(before)
            dispatched_payload = payload(before)
            sequence = await session.send_control(
                name, frame_type=frame_type, payload=dispatched_payload
            )
            after = await session.confirm_control(
                sequence, lambda state: postcondition(before, state, dispatched_payload)
            )
            return ControlResult(
                before=before, after=after, unconfirmed_settings=unconfirmed_settings
            )

    async def _read_state_batch(self) -> tuple[_StateResult, ...]:
        async with self._session(self.timeout) as session:
            result = await session.query_state()
            return (*session.drain_state_backlog(), result)

    async def _read_state_result(self) -> _StateResult:
        return (await self._read_state_batch())[-1]

    @asynccontextmanager
    async def _session(self, timeout: float) -> AsyncIterator[_LocalSession]:
        writer: asyncio.StreamWriter | None = None
        session: _LocalSession | None = None
        correlation = next(_SESSION_IDS)
        started_at = monotonic()
        unknown_logged = False

        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                session = _LocalSession(
                    reader, writer, self.device_id, correlation, started_at
                )
                yield session
        except (TimeoutError, OSError, ProtocolError, asyncio.CancelledError) as error:
            dispatched = session is not None and session.control_name is not None
            _LOGGER.debug(
                "session=%s elapsed=%.3f event=%s cause=%s incomplete_bytes=%s stage=%s context=%s",
                correlation,
                monotonic() - started_at,
                "outcome_unknown" if dispatched else "failed",
                classify_error(error),
                session.decoder.buffered_bytes if session else 0,
                "response" if session is not None else "connect",
                error.context if isinstance(error, ProtocolError) else (),
            )
            if dispatched:
                _LOGGER.warning(
                    "session=%s control outcome unknown cause=%s; not retried",
                    correlation,
                    classify_error(error),
                )
                unknown_logged = True
                if isinstance(error, asyncio.CancelledError):
                    error.control_dispatched = True
                    raise
                raise OutcomeUnknown(
                    "Control was sent exactly once; outcome unknown; not retried",
                    context=error.context if isinstance(error, ProtocolError) else (),
                ) from error
            if isinstance(error, TimeoutError):
                stage = (
                    "a state report" if session is not None else "the local connection"
                )
                raise CannotConnect(f"Timed out waiting for {stage}") from error
            if isinstance(error, OSError):
                raise CannotConnect("Local connection failed") from error
            raise
        finally:
            if writer is not None:
                writer.close()
                try:
                    with suppress(OSError):
                        await writer.wait_closed()
                except asyncio.CancelledError as error:
                    # Cleanup can replace a pending result/error after dispatch.
                    # Preserve that boundary while propagating cancellation.
                    if session is not None and session.control_name is not None:
                        error.control_dispatched = True
                        if not unknown_logged:
                            _LOGGER.warning(
                                "session=%s control outcome unknown cause=cancelled_cleanup; not retried",
                                correlation,
                            )
                    raise
            _LOGGER.debug(
                "session=%s elapsed=%.3f event=closed",
                correlation,
                monotonic() - started_at,
            )
