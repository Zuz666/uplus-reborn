"""Passive, memory-only runtime exchange evidence with bounded per-entry retention.

The coordinator serializes operations and calls this store on its event-loop
thread. Observer calls and exports are synchronous: a download cannot interleave
with an event mutation. Raw buffers are retained once and hex-encoded only in the
independent exported view. The raw-byte budget is not a total Python RAM budget.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from itertools import islice
from math import isfinite
from time import monotonic
from typing import Final

from .capture_options import DEFAULT_CAPTURE_PREFERENCES, CapturePreferences

_COUNTER_LIMIT: Final = (1 << 63) - 1
_STRING_LIMIT: Final = 512
_COLLECTION_LIMIT: Final = 32
_DEPTH_LIMIT: Final = 6
_NODE_LIMIT: Final = 192
_TEXT_LIMIT: Final = 8192


def _add(value: int, increment: int = 1) -> int:
    return min(_COUNTER_LIMIT, value + increment)


class _Normalizer:
    """Bound traversal, retained text and structure independently of raw bytes.

    Do not use asdict(), repr() or arbitrary iterable traversal: they may copy or
    inspect an unbounded input before a retention limit gets a chance to apply.
    Cycles terminate at the same depth/node limits as ordinary nested metadata.
    """

    def __init__(self) -> None:
        self.nodes = _NODE_LIMIT
        self.text = _TEXT_LIMIT
        self.omitted = 0

    def string(self, value: str) -> str:
        size = min(len(value), _STRING_LIMIT, self.text)
        self.text -= size
        if size < len(value):
            self.omitted = _add(self.omitted)
        return value[:size]

    def value(self, value: object, depth: int = 0) -> object:
        if self.nodes == 0:
            self.omitted = _add(self.omitted)
            return None
        self.nodes -= 1
        if depth >= _DEPTH_LIMIT:
            self.omitted = _add(self.omitted)
            return None
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, Enum):
            return self.value(value.value, depth + 1)
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, int):
            if -_COUNTER_LIMIT <= value <= _COUNTER_LIMIT:
                return value
            self.omitted = _add(self.omitted)
            return max(-_COUNTER_LIMIT, min(_COUNTER_LIMIT, value))
        if isinstance(value, float):
            if isfinite(value):
                return value
            self.omitted = _add(self.omitted)
            return None
        if isinstance(value, datetime):
            return self.string(value.isoformat())
        if is_dataclass(value) and not isinstance(value, type):
            members = fields(value)
            return self.mapping(
                ((member.name, getattr(value, member.name)) for member in members),
                len(members),
                depth,
            )
        if isinstance(value, dict):
            return self.mapping(value.items(), len(value), depth)
        if isinstance(value, (list, tuple)):
            retained = []
            for item in islice(value, _COLLECTION_LIMIT):
                if self.nodes == 0:
                    break
                retained.append(self.value(item, depth + 1))
            if len(retained) < len(value):
                self.omitted = _add(self.omitted, len(value) - len(retained))
            return retained
        # Bytes belong to raw(), where every retained byte is budgeted. Unknown
        # objects are never stringified (which could expose unrelated secrets).
        self.omitted = _add(self.omitted)
        return None

    def mapping(
        self, items: Iterable[tuple[object, object]], size: int, depth: int
    ) -> dict[str, object]:
        retained: dict[str, object] = {}
        if self.nodes == 0:
            self.omitted = _add(self.omitted, max(1, size))
            return retained
        self.nodes -= 1
        visited = 0
        for key, value in islice(items, _COLLECTION_LIMIT):
            if self.nodes < 2:
                break
            visited += 1
            if not isinstance(key, str):
                self.omitted = _add(self.omitted)
                continue
            self.nodes -= 1
            key = self.string(key)
            if key in retained:
                self.omitted = _add(self.omitted)
                continue
            retained[key] = self.value(value, depth + 1)
        if visited < size:
            self.omitted = _add(self.omitted, size - visited)
        return retained


@dataclass(slots=True)
class _Operation:
    kind: str
    context: dict[str, object]
    operation_id: int
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started: float = field(default_factory=monotonic)
    events: list[dict[str, object]] = field(default_factory=list)
    result: dict[str, object] | None = None
    finished_at: str | None = None
    elapsed_ms: float | None = None
    partial: bool = False
    gaps: set[str] = field(default_factory=set)
    omitted_events: int = 0
    omitted_bytes: int = 0
    omitted_metadata: int = 0
    raw_bytes: int = 0
    sequence: int = 0


class ExchangeTrace:
    """Observe one serialized entry's operations without IO or control effects."""

    def __init__(self) -> None:
        self._enabled = False
        self._closed = False
        self._preferences = DEFAULT_CAPTURE_PREFERENCES
        self._operations: deque[_Operation] = deque()
        self._current: _Operation | None = None
        # Even OFF, remember only the live scope, never an event or raw buffer.
        self._scope_kind: str | None = None
        self._scope_context: dict[str, object] = {}
        self._scope_omitted = 0
        self._events = 0
        self._raw_bytes = 0
        self._operation_id = 0
        self._omitted = self._empty_omissions()

    @staticmethod
    def _empty_omissions() -> dict[str, int]:
        return {"operations": 0, "events": 0, "raw_bytes": 0, "metadata": 0}

    @property
    def active(self) -> bool:
        """Whether protocol callers should construct diagnostic observations."""
        return self._enabled and not self._closed

    def start(self, preferences: CapturePreferences) -> None:
        """Replace evidence and effective budgets, including while already active."""
        if self._closed:
            return
        self._preferences = preferences
        self._enabled = True
        self._clear_capture()
        if self._scope_kind is not None:
            self._start_operation(partial_start=True)

    def stop(self) -> None:
        """Freeze evidence without changing an already stopped or closed trace."""
        if not self.active:
            return
        self._enabled = False
        if self._current is not None:
            self._current.partial = True
            self._current.gaps.add("capture_stopped")
            self._current = None

    def begin(self, kind: str, **context: object) -> None:
        """Enter a runtime scope, including when native DEBUG is currently OFF."""
        if self._closed:
            return
        if self._current is not None:
            self._current.partial = True
            self._current.gaps.add("operation_replaced")
        self._scope_kind = kind[:_STRING_LIMIT]
        self._scope_context = dict(islice(context.items(), _COLLECTION_LIMIT))
        self._scope_omitted = max(0, len(context) - _COLLECTION_LIMIT)
        if len(kind) > _STRING_LIMIT:
            self._scope_omitted = _add(self._scope_omitted)
        if self.active:
            self._start_operation(partial_start=False)

    def _start_operation(self, *, partial_start: bool) -> None:
        normalizer = _Normalizer()
        context = normalizer.mapping(
            self._scope_context.items(), len(self._scope_context), 0
        )
        self._scope_context = context
        self._scope_omitted = _add(self._scope_omitted, normalizer.omitted)
        self._operation_id = _add(self._operation_id)
        operation = _Operation(self._scope_kind or "", context, self._operation_id)
        if partial_start:
            operation.partial = True
            operation.gaps.add("capture_started_mid_operation")
        self._current = operation
        self._operations.append(operation)
        self._metadata_gap(operation, self._scope_omitted)
        while len(self._operations) > self._preferences.operations:
            self._evict_oldest()

    def finish(self, outcome: str, *, cause: str | None = None) -> None:
        """Record the real result only if this scope is still being captured."""
        if self._closed:
            return
        if self.active and self._current is not None:
            normalizer = _Normalizer()
            self._current.result = {
                "outcome": normalizer.string(outcome),
                "cause": normalizer.string(cause) if cause is not None else None,
            }
            self._metadata_gap(self._current, normalizer.omitted)
            self._current.finished_at = datetime.now(UTC).isoformat()
            self._current.elapsed_ms = self._elapsed(self._current)
        self._current = None
        self._scope_kind = None
        self._scope_context = {}
        self._scope_omitted = 0

    def event(self, event: str, **context: object) -> None:
        """Retain a bounded processing observation only inside an active scope."""
        if not self.active or self._current is None:
            return
        operation = self._current
        if not self._event_slot(operation):
            return
        normalizer = _Normalizer()
        record = {
            "event": normalizer.string(event),
            "context": normalizer.mapping(context.items(), len(context), 0),
            "sequence": operation.sequence,
            "elapsed_ms": self._elapsed(operation),
        }
        self._metadata_gap(operation, normalizer.omitted)
        operation.events.append(record)
        self._events += 1

    def raw(self, direction: str, data: bytes, *, stage: str) -> None:
        """Retain an exact chunk prefix; explicitly account for every lost byte."""
        if not self.active or self._current is None:
            return
        operation = self._current
        size = len(data)
        if not self._event_slot(operation):
            self._raw_gap(operation, size, reason="event_limit")
            return
        while (
            self._raw_bytes + size > self._preferences.raw_bytes
            and self._evict_oldest()
        ):
            pass
        retained = min(size, self._preferences.raw_bytes - self._raw_bytes)
        normalizer = _Normalizer()
        record = {
            "event": "raw",
            "direction": normalizer.string(direction),
            "stage": normalizer.string(stage),
            "sequence": operation.sequence,
            "elapsed_ms": self._elapsed(operation),
            "data": data[:retained],
            "byte_length": min(_COUNTER_LIMIT, size),
            "retained_bytes": retained,
            "omitted_bytes": min(_COUNTER_LIMIT, size - retained),
        }
        self._metadata_gap(operation, normalizer.omitted)
        self._raw_gap(operation, size - retained)
        operation.events.append(record)
        operation.raw_bytes += retained
        self._raw_bytes += retained
        self._events += 1

    def _event_slot(self, operation: _Operation) -> bool:
        operation.sequence = _add(operation.sequence)
        while self._events >= self._preferences.events and self._evict_oldest():
            pass
        if self._events < self._preferences.events:
            return True
        operation.partial = True
        operation.gaps.add("event_limit")
        operation.omitted_events = _add(operation.omitted_events)
        self._omitted["events"] = _add(self._omitted["events"])
        return False

    def _raw_gap(
        self, operation: _Operation, size: int, *, reason: str = "raw_byte_limit"
    ) -> None:
        if size:
            operation.partial = True
            operation.gaps.add(reason)
            operation.omitted_bytes = _add(operation.omitted_bytes, size)
            self._omitted["raw_bytes"] = _add(self._omitted["raw_bytes"], size)

    def _metadata_gap(self, operation: _Operation, size: int) -> None:
        if size:
            operation.partial = True
            operation.gaps.add("metadata_limit")
            operation.omitted_metadata = _add(operation.omitted_metadata, size)
            self._omitted["metadata"] = _add(self._omitted["metadata"], size)

    def _evict_oldest(self) -> bool:
        if not self._operations or self._operations[0] is self._current:
            return False
        operation = self._operations.popleft()
        self._events -= len(operation.events)
        self._raw_bytes -= operation.raw_bytes
        self._omitted["operations"] = _add(self._omitted["operations"])
        self._omitted["events"] = _add(self._omitted["events"], len(operation.events))
        self._omitted["raw_bytes"] = _add(
            self._omitted["raw_bytes"], operation.raw_bytes
        )
        return True

    @staticmethod
    def _elapsed(operation: _Operation) -> float:
        return round(max(0.0, monotonic() - operation.started) * 1000, 3)

    def _clear_capture(self) -> None:
        self._operations.clear()
        self._current = None
        self._events = 0
        self._raw_bytes = 0
        self._operation_id = 0
        self._omitted = self._empty_omissions()

    def close(self) -> None:
        """Permanently discard this entry's evidence, including late callbacks."""
        self._closed = True
        self._enabled = False
        self._clear_capture()
        self._scope_kind = None
        self._scope_context = {}
        self._scope_omitted = 0

    def summary(self) -> dict[str, object]:
        """Snapshot bounded accounting only, without traversing events or bytes."""
        return {
            "capture_state": (
                "closed" if self._closed else "recording" if self.active else "stopped"
            ),
            "limits": {
                "operations": self._preferences.operations,
                "events": self._preferences.events,
                "raw_bytes": self._preferences.raw_bytes,
            },
            "retained": {
                "operations": len(self._operations),
                "events": self._events,
                "raw_bytes": self._raw_bytes,
            },
            "omitted": dict(self._omitted),
        }

    def export(self) -> dict[str, object]:
        """Return an independent consistent JSON view, with no IO or mutation."""
        operations = []
        for operation in self._operations:
            events = []
            for event in operation.events:
                record = dict(event)
                if "data" in record:
                    record["data_hex"] = record.pop("data").hex()
                elif "context" in record:
                    record["context"] = _copy_json(record["context"])
                events.append(record)
            operations.append(
                {
                    "operation_id": operation.operation_id,
                    "kind": operation.kind,
                    "context": _copy_json(operation.context),
                    "started_at": operation.started_at,
                    "finished_at": operation.finished_at,
                    "elapsed_ms": operation.elapsed_ms,
                    "events": events,
                    "result": dict(operation.result) if operation.result else None,
                    "partial": operation.partial,
                    "gaps": sorted(operation.gaps),
                    "omitted": {
                        "events": operation.omitted_events,
                        "raw_bytes": operation.omitted_bytes,
                        "metadata": operation.omitted_metadata,
                    },
                }
            )
        return {
            "schema": 1,
            **self.summary(),
            "metadata_limits": {
                "string_chars": _STRING_LIMIT,
                "collection_items": _COLLECTION_LIMIT,
                "depth": _DEPTH_LIMIT,
                "context_nodes": _NODE_LIMIT,
                "text_chars_per_record": _TEXT_LIMIT,
            },
            "operations": operations,
        }


def _copy_json(value: object) -> object:
    """Copy only the already bounded JSON containers, never appliance objects."""
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value
