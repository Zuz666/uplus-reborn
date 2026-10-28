"""Finite per-entry capture preferences, independent of capture lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

CONF_DIAGNOSTIC_OPERATIONS: Final = "diagnostic_operations"
CONF_DIAGNOSTIC_EVENTS: Final = "diagnostic_events"
CONF_DIAGNOSTIC_RAW_BYTES: Final = "diagnostic_raw_bytes"
CONF_DIAGNOSTIC_DURATION: Final = "diagnostic_duration"

MAX_OPERATIONS: Final = 600
MAX_EVENTS: Final = 12000
MAX_RAW_BYTES: Final = 8388608
CAPTURE_DURATIONS: Final = (1800, 3600, 7200)


@dataclass(frozen=True, slots=True)
class CapturePreferences:
    """Budgets for one new capture, not mutable settings for a running trace."""

    operations: int = 300
    events: int = 6000
    raw_bytes: int = 4194304
    duration: int = 3600

    def __post_init__(self) -> None:
        """Keep every store caller inside finite positive budgets."""
        for value, maximum in (
            (self.operations, MAX_OPERATIONS),
            (self.events, MAX_EVENTS),
            (self.raw_bytes, MAX_RAW_BYTES),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError("Diagnostic budget is outside its finite bounds")
        if type(self.duration) is not int or self.duration not in CAPTURE_DURATIONS:
            raise ValueError("Diagnostic duration is not supported")


DEFAULT_CAPTURE_PREFERENCES: Final = CapturePreferences()


def _budget(value: object, maximum: int, default: int) -> int:
    """Reject coercions, booleans and out-of-range persisted values."""
    if type(value) is int and 0 < value <= maximum:
        return value
    return default


def capture_preferences(options: Mapping[str, object]) -> CapturePreferences:
    """Normalize invalid persisted preferences independently without mutation."""
    defaults = DEFAULT_CAPTURE_PREFERENCES
    duration = options.get(CONF_DIAGNOSTIC_DURATION)
    return CapturePreferences(
        operations=_budget(
            options.get(CONF_DIAGNOSTIC_OPERATIONS), MAX_OPERATIONS, defaults.operations
        ),
        events=_budget(options.get(CONF_DIAGNOSTIC_EVENTS), MAX_EVENTS, defaults.events),
        raw_bytes=_budget(
            options.get(CONF_DIAGNOSTIC_RAW_BYTES), MAX_RAW_BYTES, defaults.raw_bytes
        ),
        duration=(
            duration
            if type(duration) is int and duration in CAPTURE_DURATIONS
            else defaults.duration
        ),
    )


def estimated_trace_depth(preferences: CapturePreferences, poll_interval: int) -> int:
    """Estimate complete ordinary polls, not the independent capture deadline.

    The synthetic reference poll uses ten events and 266 original raw bytes.
    Commands, fragmented reads and unsolicited reports can shorten this history.
    """
    complete_polls = min(
        preferences.operations, preferences.events // 10, preferences.raw_bytes // 266
    )
    return complete_polls * poll_interval
