"""Alarm payload decoding for HWD120-B1558U `0x04` and `0x74` frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

ALARM_HEADER: Final = 0x0F5A
ALARM_BITMAP_LENGTH: Final = 14
ALARM_PAYLOAD_LENGTH: Final = 16
ALARM_CANCEL_CODE: Final = "505000"
NO_FAULT: Final = "none"
UNKNOWN_FAULT: Final = "unknown_fault"

_CODE_ALPHABET: Final = (
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
_DRUM_BIT_STRIDE: Final = 56
_PROGRAMME_END_SL: Final = 43

_FAULT_GROUPS: Final[tuple[tuple[tuple[int, ...], str], ...]] = (
    ((3,), "drying_fan_fault"),
    ((4,), "water_heating_fault"),
    (tuple(range(5, 17)), "motor_fault"),
    ((17, 28), "power_board_fault"),
    ((18, 19), "drying_heating_fault"),
    ((20,), "water_temperature_fault"),
    ((21, 22), "drying_temperature_fault"),
    ((23,), "door_not_locked"),
    ((24,), "door_not_unlocked"),
    ((25,), "motor_drive_comm_fault"),
    ((26,), "common_drive_comm_fault"),
    ((27,), "touch_screen_comm_fault"),
    ((29,), "water_level_sensor_fault"),
    ((30, 34, 35), "water_inlet_fault"),
    ((31,), "water_drain_fault"),
    ((32, 33, 36), "water_level_too_high"),
    ((37,), "unbalanced_load"),
    ((38,), "weighing_error"),
    ((39,), "drying_overweight"),
    ((40,), "main_eeprom_fault"),
    ((41,), "high_drum_temperature"),
    ((42,), "other_error"),
    ((44,), "drying_comm_fault"),
    ((45,), "leakage_alarm"),
    ((46,), "fault_f6"),
    ((47,), "fault_f5"),
    ((48,), "fault_fc4"),
    ((49,), "mainboard_comm_fault"),
    ((50,), "drive_board_duplicate_fault"),
)

_PER_DRUM_FAULT_BY_SL: Final[dict[int, str]] = dict(
    sorted(
        (sl, slug)
        for indices, slug in _FAULT_GROUPS
        for sl in indices
    )
)

FAULT_OPTIONS: Final[tuple[str, ...]] = (
    NO_FAULT,
    *dict.fromkeys(_PER_DRUM_FAULT_BY_SL.values()),
    UNKNOWN_FAULT,
)


@dataclass(frozen=True, slots=True)
class DrumAlarmState:
    """Decoded active faults and completion notice for one drum."""

    faults: tuple[str, ...] = ()
    fault_codes: tuple[str, ...] = ()
    programme_end: bool = False

    @property
    def has_fault(self) -> bool:
        """Return whether at least one fault bit is active on this drum."""
        return bool(self.faults)

    @property
    def primary_fault(self) -> str:
        """Return the primary fault slug, or ``none`` when clear."""
        return self.faults[0] if self.faults else NO_FAULT


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    """Decoded dual-drum alarm snapshot from one `0x04` or `0x74` frame."""

    upper: DrumAlarmState = field(default_factory=DrumAlarmState)
    lower: DrumAlarmState = field(default_factory=DrumAlarmState)
    bitmap_hex: str = "00" * ALARM_BITMAP_LENGTH
    raw_codes: tuple[str, ...] = (ALARM_CANCEL_CODE,)


def _format_alarm_code(slot: int) -> str:
    return (
        f"5050{_CODE_ALPHABET[slot // len(_CODE_ALPHABET)]}"
        f"{_CODE_ALPHABET[slot % len(_CODE_ALPHABET)]}"
    )


class _DrumAccumulator:
    """Collect deduplicated faults while excluding non-fault programme-end bits."""

    __slots__ = ("fault_codes", "mapped_faults", "programme_end", "saw_unmapped")

    def __init__(self) -> None:
        self.mapped_faults: list[str] = []
        self.fault_codes: list[str] = []
        self.programme_end = False
        self.saw_unmapped = False

    def add(self, canonical_slot: int, raw_code: str) -> None:
        if canonical_slot == _PROGRAMME_END_SL:
            self.programme_end = True
            return
        self.fault_codes.append(raw_code)
        slug = _PER_DRUM_FAULT_BY_SL.get(canonical_slot)
        if slug is None:
            self.saw_unmapped = True
            return
        if slug not in self.mapped_faults:
            self.mapped_faults.append(slug)

    def build(self) -> DrumAlarmState:
        faults = list(self.mapped_faults)
        if self.saw_unmapped:
            faults.append(UNKNOWN_FAULT)
        return DrumAlarmState(
            faults=tuple(faults),
            fault_codes=tuple(self.fault_codes),
            programme_end=self.programme_end,
        )


def decode_alarm_payload(payload: bytes) -> AlarmSnapshot:
    """Decode one `0f5a` + 14-byte alarm bitmap into per-drum alarm states."""
    if len(payload) != ALARM_PAYLOAD_LENGTH:
        raise ValueError(
            f"Alarm payload must contain {ALARM_PAYLOAD_LENGTH} bytes, got {len(payload)}"
        )
    header = int.from_bytes(payload[:2], "big")
    if header != ALARM_HEADER:
        raise ValueError(f"Unexpected alarm payload header {header:04x}")

    bitmap = payload[2:]
    upper = _DrumAccumulator()
    lower = _DrumAccumulator()
    raw_codes: list[str] = []

    for byte_offset in range(ALARM_BITMAP_LENGTH):
        byte_idx = ALARM_BITMAP_LENGTH - 1 - byte_offset
        octet = bitmap[byte_idx]
        if not octet:
            continue
        for bit in range(8):
            if not (octet & (1 << bit)):
                continue
            bit_pos = byte_offset * 8 + bit
            slot = bit_pos + 1
            raw_code = _format_alarm_code(slot)
            raw_codes.append(raw_code)
            if slot <= _DRUM_BIT_STRIDE:
                upper.add(slot, raw_code)
            else:
                lower.add(slot - _DRUM_BIT_STRIDE, raw_code)

    return AlarmSnapshot(
        upper=upper.build(),
        lower=lower.build(),
        bitmap_hex=bitmap.hex(),
        raw_codes=tuple(raw_codes) if raw_codes else (ALARM_CANCEL_CODE,),
    )
