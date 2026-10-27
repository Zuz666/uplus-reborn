"""Behavior of the bounded, opt-in exchange trace store's public interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import pytest

from custom_components.uplus_reborn.exchange_trace import ExchangeTrace


def test_capture_preserves_raw_chunks_and_stopped_downloads() -> None:
    """Raw chunks are exact ordered observations, not reconstructed frames."""
    trace = ExchangeTrace()
    assert not trace.active
    trace.set_enabled(True)
    trace.begin("poll", host="192.0.2.10", device_id="SYNTHETIC-DEVICE")
    trace.raw("tx", b"\x00\xff\x7d", stage="write_submitted")
    trace.event("write_drained")
    trace.raw("rx", b"\x7d", stage="read")
    trace.raw("rx", b"\x01\x00\xff\x00", stage="read")
    trace.finish("success")
    trace.set_enabled(False)

    exported = trace.export()
    assert exported["capture_state"] == "stopped"
    assert exported["retained"] == {
        "operations": 1,
        "events": 4,
        "raw_bytes": 8,
    }
    operation = exported["operations"][0]
    assert operation["context"]["device_id"] == "SYNTHETIC-DEVICE"
    assert operation["result"] == {"outcome": "success", "cause": None}
    assert not operation["partial"]
    assert [event["event"] for event in operation["events"]] == [
        "raw",
        "write_drained",
        "raw",
        "raw",
    ]
    raw = [event for event in operation["events"] if event["event"] == "raw"]
    assert [event["data_hex"] for event in raw] == ["00ff7d", "7d", "0100ff00"]
    assert [event["byte_length"] for event in raw] == [3, 1, 4]
    assert [event["stage"] for event in raw] == ["write_submitted", "read", "read"]
    assert all(event["omitted_bytes"] == 0 for event in raw)
    assert json.loads(json.dumps(exported)) == exported
    assert trace.export() == exported
    exported["operations"][0]["events"][0]["data_hex"] = "mutated"
    assert trace.export()["operations"][0]["events"][0]["data_hex"] == "00ff7d"


def _assert_retention_matches_evidence(exported: dict) -> None:
    """Validate counters from the visible evidence, not store internals."""
    operations = exported["operations"]
    events = [event for operation in operations for event in operation["events"]]
    raw = [event for event in events if event["event"] == "raw"]
    assert exported["retained"] == {
        "operations": len(operations),
        "events": len(events),
        "raw_bytes": sum(len(bytes.fromhex(event["data_hex"])) for event in raw),
    }
    for resource in ("operations", "events", "raw_bytes"):
        assert exported["retained"][resource] <= exported["limits"][resource]
    for event in raw:
        assert event["retained_bytes"] == len(bytes.fromhex(event["data_hex"]))
        assert event["retained_bytes"] + event["omitted_bytes"] == event["byte_length"]


def _assert_metadata_bounds(value: object, limits: dict) -> None:
    """Count only the public normalized tree, including dictionary key nodes."""
    def measure(item: object, depth: int) -> tuple[int, int]:
        assert depth <= limits["depth"]
        if isinstance(item, str):
            assert len(item) <= limits["string_chars"]
            return 1, len(item)
        if isinstance(item, dict):
            assert len(item) <= limits["collection_items"]
            nodes, text = 1, 0
            for key, child in item.items():
                assert len(key) <= limits["string_chars"]
                child_nodes, child_text = measure(child, depth + 1)
                nodes += 1 + child_nodes
                text += len(key) + child_text
            return nodes, text
        if isinstance(item, list):
            assert len(item) <= limits["collection_items"]
            sizes = [measure(child, depth + 1) for child in item]
            return 1 + sum(size[0] for size in sizes), sum(size[1] for size in sizes)
        return 1, 0

    nodes, text = measure(value, 0)
    assert nodes <= limits["context_nodes"]
    assert text <= limits["text_chars_per_record"]


def test_all_three_limits_at_and_beyond_the_same_boundary() -> None:
    """An added operation evicts the entire oldest one, not arbitrary events."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    for number in range(300):
        trace.begin("poll", number=number)
        size = 13982 if number < 4 else 13981
        trace.raw("rx", b"x" * size, stage="read")
        for _ in range(19):
            trace.event("observation")
        trace.finish("success")
    boundary = trace.export()
    assert boundary["retained"] == {
        "operations": 300,
        "events": 6000,
        "raw_bytes": 4194304,
    }
    assert boundary["omitted"] == {
        "operations": 0,
        "events": 0,
        "raw_bytes": 0,
        "metadata": 0,
    }
    _assert_retention_matches_evidence(boundary)

    trace.begin("control", number=300)
    trace.raw("tx", b"y" * 13982, stage="write_submitted")
    for _ in range(19):
        trace.event("observation")
    trace.finish("confirmed")
    exported = trace.export()
    assert exported["retained"] == boundary["retained"]
    assert [operation["context"]["number"] for operation in exported["operations"]] == list(
        range(1, 301)
    )
    assert exported["omitted"] == {
        "operations": 1,
        "events": 20,
        "raw_bytes": 13982,
        "metadata": 0,
    }
    assert all(not operation["partial"] for operation in exported["operations"])
    _assert_retention_matches_evidence(exported)


def test_operation_pressure_alone_preserves_newest_complete_operations() -> None:
    """Small exchanges hit the operation bound without event or byte pressure."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    for number in range(301):
        trace.begin("poll", number=number)
        trace.raw("rx", bytes([number % 256]), stage="read")
        trace.finish("received")
    exported = trace.export()
    assert [op["context"]["number"] for op in exported["operations"]] == list(range(1, 301))
    assert exported["retained"] == {"operations": 300, "events": 300, "raw_bytes": 300}
    assert exported["omitted"] == {
        "operations": 1, "events": 1, "raw_bytes": 1, "metadata": 0,
    }
    assert all(not op["partial"] and op["result"]["outcome"] == "received"
               for op in exported["operations"])
    _assert_retention_matches_evidence(exported)


def test_event_pressure_evicts_oldest_operation_before_current_events() -> None:
    """Event pressure alone applies the same oldest-operation retention rule."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("older")
    trace.event("first")
    trace.event("second")
    trace.finish("success")
    trace.begin("current")
    for index in range(5999):
        trace.event("observed", index=index)
    trace.finish("success")
    exported = trace.export()
    assert [operation["kind"] for operation in exported["operations"]] == ["current"]
    assert exported["retained"]["events"] == 5999
    assert exported["omitted"]["operations"] == 1
    assert exported["omitted"]["events"] == 2
    assert not exported["operations"][0]["partial"]
    assert exported["operations"][0]["events"][0]["context"] == {"index": 0}
    _assert_retention_matches_evidence(exported)


def test_raw_pressure_evicts_oldest_operation_before_truncating_current() -> None:
    """Byte pressure alone frees an old operation, including all its metadata."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("older", expectation="old")
    trace.raw("rx", b"a" * 2097152, stage="read")
    trace.event("old_decision")
    trace.finish("success")
    trace.begin("current")
    trace.raw("rx", b"b" * 2097152, stage="read")
    assert trace.export()["retained"]["raw_bytes"] == 4194304
    trace.raw("rx", b"c", stage="read")
    trace.finish("success")
    exported = trace.export()
    assert [operation["kind"] for operation in exported["operations"]] == ["current"]
    assert exported["retained"] == {
        "operations": 1,
        "events": 2,
        "raw_bytes": 2097153,
    }
    assert exported["omitted"]["operations"] == 1
    assert exported["omitted"]["events"] == 2
    assert exported["omitted"]["raw_bytes"] == 2097152
    assert not exported["operations"][0]["partial"]
    assert exported["operations"][0]["events"][1]["data_hex"] == "63"
    _assert_retention_matches_evidence(exported)


def test_oversized_operation_keeps_prefix_context_result_and_honest_gaps() -> None:
    """A single noisy exchange cannot exceed either global event or byte limit."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("control", requested_intent={"program": "spin"})
    payload = b"\x00\xff" * 2097152 + b"omitted-17-bytes!"
    assert len(payload) == 4194321
    trace.raw("rx", payload, stage="read")
    trace.raw("rx", b"missing", stage="read")
    for _ in range(5998):
        trace.event("confirmation_pending")
    trace.event("not_retained")
    trace.raw("rx", b"end", stage="read")
    trace.finish("unknown", cause="timeout")
    exported = trace.export()
    assert exported["retained"] == {
        "operations": 1,
        "events": 6000,
        "raw_bytes": 4194304,
    }
    operation = exported["operations"][0]
    assert operation["partial"]
    assert set(operation["gaps"]) == {"raw_byte_limit", "event_limit"}
    assert operation["context"] == {"requested_intent": {"program": "spin"}}
    assert operation["result"] == {"outcome": "unknown", "cause": "timeout"}
    assert operation["omitted"] == {"events": 2, "raw_bytes": 27, "metadata": 0}
    assert exported["omitted"] == {
        "operations": 0,
        "events": 2,
        "raw_bytes": 27,
        "metadata": 0,
    }
    first, second = operation["events"][:2]
    assert bytes.fromhex(first["data_hex"]) == payload[:4194304]
    assert first["byte_length"] == len(payload)
    assert first["retained_bytes"] == 4194304
    assert first["omitted_bytes"] == 17
    assert second["data_hex"] == ""
    assert second["byte_length"] == second["omitted_bytes"] == 7
    assert second["retained_bytes"] == 0
    _assert_retention_matches_evidence(exported)


def test_event_limit_also_accounts_for_unretained_raw_chunk() -> None:
    """Raw loss due to event exhaustion is not falsely called byte exhaustion."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("poll")
    for _ in range(6000):
        trace.event("observed")
    trace.raw("rx", b"unretained", stage="read")
    trace.finish("success")
    operation = trace.export()["operations"][0]
    assert operation["gaps"] == ["event_limit"]
    assert operation["omitted"] == {"events": 1, "raw_bytes": 10, "metadata": 0}
    assert operation["partial"]
    assert operation["result"]["outcome"] == "success"


def test_repeated_identical_raw_buffers_each_consume_the_byte_budget() -> None:
    """Identical buffers are separate observations, not free deduplicated bytes."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("poll")
    payload = b"z" * 2097152
    trace.raw("rx", payload, stage="read")
    trace.raw("rx", payload, stage="read")
    trace.raw("rx", payload, stage="read")
    trace.raw("rx", b"", stage="read")
    trace.finish("success")
    exported = trace.export()
    assert exported["retained"]["raw_bytes"] == 4194304
    assert exported["operations"][0]["omitted"]["raw_bytes"] == 2097152
    assert [event["retained_bytes"] for event in exported["operations"][0]["events"]] == [
        2097152,
        2097152,
        0,
        0,
    ]
    _assert_retention_matches_evidence(exported)


class _DoNotInspect:
    def __repr__(self) -> str:
        raise AssertionError("trace inspected an unsupported object")


def test_off_observations_and_late_finish_do_not_change_frozen_capture() -> None:
    """OFF observer methods do no metadata normalization or byte inspection."""
    trace = ExchangeTrace()
    trace.begin("off", context=_DoNotInspect())
    trace.event("off", context=_DoNotInspect())
    trace.raw("rx", _DoNotInspect(), stage="read")
    trace.finish("ignored")
    assert trace.export()["operations"] == []
    trace.set_enabled(True)
    trace.begin("control")
    trace.raw("tx", b"sent", stage="write_submitted")
    trace.set_enabled(False)
    stopped = trace.export()
    assert stopped["operations"][0]["partial"]
    assert stopped["operations"][0]["gaps"] == ["capture_stopped"]
    assert stopped["operations"][0]["result"] is None
    trace.event("late", context=_DoNotInspect())
    trace.raw("rx", _DoNotInspect(), stage="read")
    trace.finish("confirmed")
    trace.begin("off", context=_DoNotInspect())
    trace.finish("ignored")
    assert trace.export() == stopped


def test_enable_mid_operation_has_no_fabricated_earlier_bytes() -> None:
    """Starting capture in flight retains known scope but explicitly lacks start."""
    trace = ExchangeTrace()
    trace.begin("control", host="192.0.2.10", requested_intent={"option": "power"})
    trace.raw("tx", b"not captured", stage="write_submitted")
    trace.set_enabled(True)
    trace.raw("rx", b"captured", stage="read")
    trace.finish("confirmed")
    exported = trace.export()
    operation = exported["operations"][0]
    assert operation["kind"] == "control"
    assert operation["context"] == {
        "host": "192.0.2.10",
        "requested_intent": {"option": "power"},
    }
    assert operation["partial"]
    assert operation["gaps"] == ["capture_started_mid_operation"]
    assert operation["result"] == {"outcome": "confirmed", "cause": None}
    assert [event["data_hex"] for event in operation["events"]] == ["6361707475726564"]
    assert exported["retained"]["raw_bytes"] == 8
    # Earlier byte/event counts are unknown, not invented as a zero-loss history.
    assert operation["omitted"]["raw_bytes"] == 0


def test_stop_restart_in_same_operation_creates_fresh_partial_segment() -> None:
    """A replacement capture discards the earlier segment, even in one scope."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("control", target="upper")
    trace.raw("tx", b"old", stage="write_submitted")
    trace.set_enabled(False)
    trace.set_enabled(True)
    resumed = trace.export()["operations"][0]
    assert resumed["context"] == {"target": "upper"}
    assert resumed["events"] == []
    assert resumed["partial"]
    assert resumed["gaps"] == ["capture_started_mid_operation"]
    trace.raw("rx", b"new", stage="read")
    trace.finish("unknown", cause="cancelled")
    exported = trace.export()
    assert exported["retained"] == {"operations": 1, "events": 1, "raw_bytes": 3}
    assert exported["operations"][0]["events"][0]["data_hex"] == "6e6577"
    assert exported["operations"][0]["result"] == {
        "outcome": "unknown",
        "cause": "cancelled",
    }


def test_transition_only_reset_and_empty_fresh_capture() -> None:
    """Same-state logger notifications do nothing; OFF/ON always clears."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("poll")
    trace.event("observed")
    trace.finish("success")
    recording = trace.export()
    trace.set_enabled(True)
    assert trace.export() == recording
    trace.set_enabled(False)
    stopped = trace.export()
    trace.set_enabled(False)
    assert trace.export() == stopped
    trace.set_enabled(True)
    assert trace.export()["operations"] == []
    assert trace.export()["retained"] == {"operations": 0, "events": 0, "raw_bytes": 0}
    assert not any(trace.export()["omitted"].values())
    trace.set_enabled(False)
    trace.set_enabled(True)
    assert trace.export()["operations"] == []


def test_close_clears_permanently_despite_old_callbacks_and_toggles() -> None:
    """No reference to an unloaded store can repopulate or restart its capture."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("control", target="upper")
    trace.raw("tx", b"old", stage="write_submitted")
    trace.close()
    closed = trace.export()
    assert not trace.active
    assert closed["capture_state"] == "closed"
    assert closed["operations"] == []
    assert closed["retained"] == {"operations": 0, "events": 0, "raw_bytes": 0}
    trace.finish("confirmed")
    trace.set_enabled(True)
    trace.begin("late", context=_DoNotInspect())
    trace.event("late", context=_DoNotInspect())
    trace.raw("rx", _DoNotInspect(), stage="read")
    trace.finish("late")
    trace.set_enabled(False)
    trace.close()
    assert trace.export() == closed


def test_entry_captures_have_independent_retention_and_lifecycle() -> None:
    """A domain-wide enable does not share identities, budgets or cleanup."""
    first, second = ExchangeTrace(), ExchangeTrace()
    for trace, device in ((first, "FIRST"), (second, "SECOND")):
        trace.set_enabled(True)
        trace.begin("poll", device_id=device)
        trace.raw("rx", device.encode(), stage="read")
        trace.finish("success")
    preserved = second.export()
    first.set_enabled(False)
    first.set_enabled(True)
    first.close()
    assert second.export() == preserved
    assert preserved["operations"][0]["context"]["device_id"] == "SECOND"
    assert preserved["retained"]["raw_bytes"] == 6


class _Target(Enum):
    UPPER = "upper"


@dataclass
class _Snapshot:
    observed_at: datetime
    target: _Target
    settings: tuple[int, ...]


def test_metadata_normalizes_snapshots_once_and_exports_independent_values() -> None:
    """Original dataclasses and caller-owned nested containers are not retained."""
    trace = ExchangeTrace()
    snapshot = _Snapshot(datetime(2026, 10, 28, 12, tzinfo=UTC), _Target.UPPER, (1, 2))
    intent = {"settings": [1, 2]}
    trace.set_enabled(True)
    trace.begin("control", cached_snapshot=snapshot, requested_intent=intent)
    trace.event("state_observed", decoded_snapshot=snapshot)
    snapshot.settings = (9,)
    intent["settings"].append(3)
    trace.finish("not_dispatched", cause="state_changed")
    exported = trace.export()
    normalized = {
        "observed_at": "2026-10-28T12:00:00+00:00",
        "target": "upper",
        "settings": [1, 2],
    }
    operation = exported["operations"][0]
    assert operation["context"]["cached_snapshot"] == normalized
    assert operation["context"]["requested_intent"] == {"settings": [1, 2]}
    assert operation["events"][0]["context"]["decoded_snapshot"] == normalized
    assert not operation["partial"]
    operation["context"]["cached_snapshot"]["settings"].append(8)
    operation["events"][0]["context"]["decoded_snapshot"]["settings"].append(7)
    operation["result"]["outcome"] = "mutated"
    preserved = trace.export()["operations"][0]
    assert preserved["context"]["cached_snapshot"] == normalized
    assert preserved["events"][0]["context"]["decoded_snapshot"] == normalized
    assert preserved["result"]["outcome"] == "not_dispatched"


@pytest.mark.parametrize(
    "value",
    [
        "x" * 100000,
        list(range(100000)),
        {f"key-{index}": index for index in range(1000)},
        [[["x" * 10000] * 50] * 50] * 50,
        10**10000,
        float("inf"),
        float("nan"),
        b"unbudgeted raw bytes",
        _DoNotInspect(),
    ],
    ids=["text", "list", "mapping", "nested", "integer", "infinity", "nan", "bytes", "object"],
)
def test_metadata_has_its_own_limits_and_explicit_omissions(value: object) -> None:
    """Metadata pressure never consumes unlimited memory or erases the outcome."""
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin("poll", observed=value)
    trace.event("observed", decoded=value)
    trace.finish("success")
    exported = trace.export()
    serialized = json.dumps(exported, allow_nan=False)
    assert len(serialized) < 100000
    operation = exported["operations"][0]
    assert operation["partial"]
    assert "metadata_limit" in operation["gaps"]
    assert operation["omitted"]["metadata"] > 0
    assert exported["omitted"]["metadata"] == operation["omitted"]["metadata"]
    assert operation["result"] == {"outcome": "success", "cause": None}
    assert exported["retained"]["raw_bytes"] == 0
    _assert_metadata_bounds(operation["context"], exported["metadata_limits"])
    _assert_metadata_bounds(
        operation["events"][0]["context"], exported["metadata_limits"]
    )


def test_cyclic_metadata_and_truncated_key_collisions_terminate_honestly() -> None:
    """Cyclic containers and colliding truncated keys cannot grow the trace."""
    cycle = []
    cycle.append(cycle)
    trace = ExchangeTrace()
    trace.set_enabled(True)
    trace.begin(
        "k" * 100000,
        cyclic=cycle,
        mapping={"x" * 1000 + "a": "first", "x" * 1000 + "b": "second"},
    )
    trace.event("e" * 100000, cyclic=cycle)
    trace.finish("o" * 100000, cause="c" * 100000)
    exported = trace.export()
    operation = exported["operations"][0]
    assert len(operation["kind"]) <= exported["metadata_limits"]["string_chars"]
    assert len(operation["events"][0]["event"]) <= 512
    assert len(operation["result"]["outcome"]) <= 512
    assert len(operation["result"]["cause"]) <= 512
    assert len(operation["context"]["mapping"]) == 1
    assert operation["partial"]
    assert operation["omitted"]["metadata"] >= 6
    assert len(json.dumps(exported, allow_nan=False)) < 10000
