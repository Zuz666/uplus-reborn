from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.uplus_reborn.protocol import (
    CannotConnect,
    ControlResult,
    DeviceSnapshot,
    InvalidFrame,
    OutcomeUnknown,
    StateChanged,
    UnsafeState,
    UwtClient,
)
from custom_components.uplus_reborn.protocol.__main__ import _inspect_inner
from custom_components.uplus_reborn.protocol._capabilities import (
    Drum,
    program_delay_capabilities,
)
from custom_components.uplus_reborn.protocol._commands import (
    default_intent,
    fabric_intent,
    update_intent,
)
from custom_components.uplus_reborn.protocol._protocol import (
    _build_heartbeat_request,
    _build_inner_frame,
    _decode_inner_frame,
    _decode_state,
    _EnvelopeDecoder,
    _state_words,
    validate_drum_control,
    validate_program_start,
)


class _RecordingObserver:
    """Public observer seam retaining synthetic loopback observations."""

    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.events: list[tuple[str, dict[str, object]]] = []
        self.raw_chunks: list[tuple[str, bytes, str]] = []
        self.order: list[str] = []

    def event(self, event: str, **context: object) -> None:
        if not self.active:
            raise AssertionError("Inactive observer received an event")
        self.events.append((event, context))
        self.order.append(event)

    def raw(self, direction: str, data: bytes, *, stage: str) -> None:
        if not self.active:
            raise AssertionError("Inactive observer received raw bytes")
        self.raw_chunks.append((direction, data, stage))
        self.order.append(f"raw_{direction}")


def _inner_frame(frame_type: int, payload: bytes) -> bytes:
    size = 8 + len(payload)
    body = bytes((size, 0, 0, 0, 0, 0, 0, frame_type)) + payload
    escaped = body.replace(b"\xff", b"\xff\x55")
    checksum = bytes((sum(escaped) & 0xFF,)).replace(b"\xff", b"\xff\x55")
    return b"\xff\xff" + escaped + checksum


def _frame_response(frame_type: int, payload: bytes, *, sequence: int = 0) -> bytes:
    inner = _inner_frame(frame_type, payload)
    return (
        struct.pack(
            ">II32s32sII",
            10005,
            0,
            b"",
            b"TESTDEVICE",
            sequence,
            len(inner),
        )
        + inner
    )


def _state_response(
    words: list[int], *, sequence: int = 0, frame_type: int = 0x06
) -> bytes:
    return _frame_response(
        frame_type,
        b"".join(word.to_bytes(2, "big") for word in words),
        sequence=sequence,
    )


def _snapshot(words: list[int]) -> DeviceSnapshot:
    frame, consumed = _decode_inner_frame(
        _inner_frame(0x06, b"".join(word.to_bytes(2, "big") for word in words))
    )
    if consumed == 0:
        raise AssertionError("Test fixture did not contain an inner frame")
    return _decode_state(_state_words(frame), datetime.now(UTC))


def _standby_words(*, program: int = 0, remote_control: bool = True) -> list[int]:
    words = [0] * 41
    words[0] = 0x6D01
    words[1] = program << 8
    words[3] = 0x0003 if remote_control else 0x0001
    words[26] = 0x0021 if remote_control else 0x0001
    return words


def _active_spin_words(*, phase: int = 0x0B) -> list[int]:
    words = _standby_words(program=0x1C)
    words[3] = phase << 8 | 0x00A3
    words[5] = 0x0600
    words[15] = 2
    return words


def _cancelled_spin_words(*, locked: bool) -> list[int]:
    words = _standby_words(program=0x1C)
    words[3] = 0x0023 if locked else 0x0003
    words[5] = 0x0600
    return words


def _cycle_words(
    drum: Drum, *, program: int = 0x1C, phase: int = 0x0B, active: bool = True
) -> list[int]:
    words = _standby_words()
    program_word, phase_word, minute_word = (
        (1, 3, 5) if drum == "upper" else (24, 26, 28)
    )
    words[program_word] = program << 8
    words[phase_word] = phase << 8 | 0x23 | (0x80 if active else 0)
    words[minute_word] = 0x0600
    words[15 if drum == "upper" else 32] = 2
    return words


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[int, bytes]:
    header = await reader.readexactly(80)
    payload_length = int.from_bytes(header[76:80], "big")
    payload = await reader.readexactly(payload_length)
    return int.from_bytes(header[72:76], "big"), payload


async def _accept_control_initialization(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> tuple[int, bytes]:
    request = await _read_request(reader)
    writer.write(
        _frame_response(
            0x74,
            bytes.fromhex("0f5a0000000000000000000000000000"),
            sequence=1,
        )
    )
    await writer.drain()
    return request


def _cli_start_success_cases() -> list[tuple[str, list[str], str, list[int]]]:
    """Independent command literals already established by the loopback proofs."""
    smart = _cycle_words("upper", program=0x13, phase=0x05)
    smart[2], smart[14], smart[15], smart[16] = 0x0200, 2, 2, 20
    return [
        (
            "ordinary_lower",
            ["--drum", "lower", "--program", "spin", "--spin-rpm", "400"],
            "00021c000000000000000190000200000000000000000000",
            _cycle_words("lower"),
        ),
        (
            "smart_upper",
            ["--drum", "upper", "--fabric", "A1", "--stain", "W30"],
            "000113000002000000140000020200000000000000000000",
            smart,
        ),
    ]


def _cli_start_rejection_cases() -> list[tuple[str, list[str], int, tuple[str, ...]]]:
    ready = ["--allow-live", "--confirm-appliance-ready", "--drum", "upper"]
    smart_override = ["--fabric", "A1", "--delay-half-hour-step", "0"]
    return [
        ("smart_zero_override", ready + smart_override, 1, ("smart", "manual")),
        (
            "smart_function_override",
            ready + ["--fabric", "A1", "--function", "eco"],
            1,
            ("smart", "manual"),
        ),
        (
            "stain_without_fabric",
            ready + ["--program", "spin", "--stain", "W1"],
            1,
            ("stain", "fabric"),
        ),
        (
            "stain_parser_vocabulary",
            ready + ["--fabric", "A1", "--stain", "grass"],
            2,
            ("invalid choice", "stain"),
        ),
        (
            "mutually_exclusive_selection",
            ready + ["--program", "spin", "--fabric", "A1"],
            2,
            ("not allowed",),
        ),
        (
            "unsupported_program",
            ready + ["--program", "not-a-program"],
            1,
            ("program",),
        ),
        (
            "missing_opt_in_precedes_preparation",
            ["--confirm-appliance-ready", "--drum", "upper", *smart_override],
            1,
            ("allow-live",),
        ),
        (
            "missing_readiness_precedes_preparation",
            ["--allow-live", "--drum", "upper", *smart_override],
            1,
            ("confirm-appliance-ready",),
        ),
    ]


async def _exercise_cli_start(
    options: list[str],
    *,
    expected_group: str | None = None,
    after: list[int] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Run the real entry point; allow only one exact bounded synthetic transaction."""
    root = (source_root or Path(__file__).resolve().parents[1]).resolve()
    expected = (
        [
            (0, _inner_frame(0x01, b"\x4d\x01")),
            (1, _inner_frame(0x73, b"")),
            (2, _inner_frame(0x60, bytes.fromhex(expected_group))),
        ]
        if expected_group is not None
        else []
    )
    requests: list[tuple[int, bytes]] = []
    errors: list[str] = []
    tasks: set[asyncio.Task] = set()
    connections = 0
    peer_confirmed = False
    trailing = b""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections, peer_confirmed, trailing
        connections += 1
        try:
            if not expected or connections != 1:
                raise AssertionError("Unexpected CLI connection")
            for index, wanted in enumerate(expected):
                observed = await asyncio.wait_for(_read_request(reader), 5)
                requests.append(observed)
                if observed != wanted:
                    raise AssertionError(f"Unexpected CLI request at position {index}")
                if index == 0:
                    writer.write(_state_response(_standby_words(), frame_type=0x02))
                elif index == 1:
                    writer.write(
                        _frame_response(
                            0x74,
                            bytes.fromhex("0f5a0000000000000000000000000000"),
                            sequence=1,
                        )
                    )
                else:
                    if after is None:
                        raise AssertionError("Missing synthetic confirmation")
                    writer.write(
                        _frame_response(0x03, b"\x00\x00", sequence=2)
                        + _state_response(after)
                    )
                    peer_confirmed = True
                await writer.drain()
            trailing = await asyncio.wait_for(reader.read(), 5)
            if trailing:
                raise AssertionError("Unexpected traffic after the single Start")
        except (AssertionError, OSError, ValueError, asyncio.IncompleteReadError) as error:
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, RuntimeError) as error:
                errors.append(f"Peer cleanup {type(error).__name__}: {error}")

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    command = [
        sys.executable,
        "-m",
        "custom_components.uplus_reborn.protocol",
        "start",
        "--host",
        "127.0.0.1",
        "--port",
        str(server.sockets[0].getsockname()[1]),
        "--device-id",
        "TESTDEVICE",
        "--timeout",
        "1",
        "--control-timeout",
        "3",
        *options,
    ]
    environment = dict(os.environ)
    environment.pop("UPLUS_REBORN_DEVICE_ID", None)
    environment["PYTHONPATH"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = None
    stdout = stderr = b""
    timed_out = False
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    except TimeoutError:
        if process is None:
            raise
        timed_out = True
        errors.append("CLI subprocess exceeded its bounded deadline")
    finally:
        try:
            if process is not None and process.returncode is None:
                process.kill()
                stdout, stderr = await process.communicate()
        except (OSError, RuntimeError) as error:
            errors.append(f"Subprocess cleanup {type(error).__name__}: {error}")
        try:
            server.close()
            await server.wait_closed()
        except (OSError, RuntimeError) as error:
            errors.append(f"Server cleanup {type(error).__name__}: {error}")
        finally:
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), 6
                    )
                except (TimeoutError, RuntimeError) as error:
                    errors.append(f"Peer cleanup {type(error).__name__}: {error}")
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "command": command,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout.decode("utf-8"),
        "stderr": stderr.decode("utf-8"),
        "connections": connections,
        "expected_requests": [
            {"sequence": sequence, "inner_hex": payload.hex()}
            for sequence, payload in expected
        ],
        "observed_requests": [
            {"sequence": sequence, "inner_hex": payload.hex()}
            for sequence, payload in requests
        ],
        "expected_group_payload_hex": expected_group,
        "peer_confirmed": peer_confirmed,
        "peer_errors": errors,
        "trailing_hex": trailing.hex(),
        "cleanup": {
            "subprocess_reaped": process.returncode is not None,
            "server_closed": not server.is_serving(),
            "peer_tasks_done": all(task.done() for task in tasks),
        },
    }


@pytest.mark.usefixtures("loopback_socket")
class ProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_core_action_peer_proves_one_bounded_transaction(self) -> None:
        """The Core fixture supplies both acceptance and independent state proof."""
        from tools.ha_smoke import DEVICE_ID, ProgrammeIntentPeer

        peer = ProgrammeIntentPeer()
        await peer.start()
        try:
            peer.arm("ordinary")
            client = UwtClient(
                "127.0.0.1", DEVICE_ID, port=peer.port, timeout=1, control_timeout=2
            )
            result = await client.start_program(
                update_intent(default_intent("lower", "spin"), spin_rpm=400),
                allow_live=True,
            )
            self.assertTrue(result.after.lower.cycle_active)
            self.assertEqual([query["sequence"] for query in peer.queries], [0])
            self.assertEqual(len(peer.action_transactions), 1)
            self.assertEqual(
                peer.action_transactions[0]["grouped_start_payload"],
                "00021c000000000000000190000200000000000000000000",
            )
            self.assertEqual(peer.rejected, [])
        finally:
            await peer.close()
        self.assertTrue(peer.evidence()["closed"])

    async def test_cli_start_prepares_ordinary_and_smart_commands(self) -> None:
        for name, options, expected_group, after in _cli_start_success_cases():
            with self.subTest(case=name):
                receipt = await _exercise_cli_start(
                    ["--allow-live", "--confirm-appliance-ready", *options],
                    expected_group=expected_group,
                    after=after,
                )
                self.assertEqual(receipt["returncode"], 0, receipt["stderr"])
                self.assertEqual(receipt["stderr"], "")
                self.assertEqual(receipt["connections"], 1)
                self.assertEqual(receipt["peer_errors"], [])
                self.assertEqual(
                    receipt["observed_requests"], receipt["expected_requests"]
                )
                self.assertTrue(receipt["peer_confirmed"])
                self.assertEqual(receipt["trailing_hex"], "")
                self.assertTrue(all(receipt["cleanup"].values()))
                result = json.loads(receipt["stdout"])
                self.assertEqual(result["intent"], "start")
                drum = "lower" if name == "ordinary_lower" else "upper"
                self.assertEqual(result["result"]["before"][drum]["phase_value"], 0)
                observed = result["result"]["after"][drum]
                self.assertTrue(observed["cycle_active"])
                self.assertTrue(observed["door_locked"])
                self.assertEqual(
                    observed["program_value"], 0x1C if drum == "lower" else 0x13
                )
                self.assertEqual(
                    result["result"]["unconfirmed_settings"],
                    ["spin_rpm"] if drum == "lower" else [],
                )
                if drum == "upper":
                    self.assertTrue(observed["reported_settings"]["smart"])
                    self.assertEqual(
                        observed["reported_settings"]["wash_minutes_encoded"], 20
                    )

    async def test_cli_start_rejections_preserve_exits_and_prevent_network(self) -> None:
        for name, options, status, cause in _cli_start_rejection_cases():
            with self.subTest(case=name):
                receipt = await _exercise_cli_start(options)
                self.assertEqual(receipt["returncode"], status, receipt["stderr"])
                self.assertEqual(receipt["stdout"], "")
                for fragment in cause:
                    self.assertIn(fragment, receipt["stderr"].lower())
                self.assertNotIn("Traceback", receipt["stderr"])
                if status == 1:
                    self.assertTrue(receipt["stderr"].startswith("error: "))
                else:
                    self.assertIn("usage:", receipt["stderr"])
                self.assertEqual(receipt["connections"], 0)
                self.assertEqual(receipt["observed_requests"], [])
                self.assertEqual(receipt["peer_errors"], [])
                self.assertFalse(receipt["peer_confirmed"])
                self.assertTrue(all(receipt["cleanup"].values()))

    def test_inner_frame_stuffs_body_and_checksum_octets(self) -> None:
        stuffed_body = _build_inner_frame(0x01, b"\xff\x01")
        self.assertEqual(
            stuffed_body,
            bytes.fromhex("ffff0a00000000000001ff550160"),
        )
        frame, consumed = _decode_inner_frame(stuffed_body)
        self.assertEqual(consumed, len(stuffed_body))
        self.assertEqual(frame.frame_type, 0x01)
        self.assertEqual(frame.payload, b"\xff\x01")

        stuffed_checksum = _build_inner_frame(0x01, b"\xf5")
        self.assertTrue(stuffed_checksum.endswith(b"\xff\x55"))
        frame, consumed = _decode_inner_frame(stuffed_checksum)
        self.assertEqual(consumed, len(stuffed_checksum))
        self.assertEqual(frame.payload, b"\xf5")

    def test_inner_frame_rejects_unescaped_body_ff(self) -> None:
        malformed = bytes.fromhex("ffff0a00000000000001ff0160")
        with self.assertRaisesRegex(InvalidFrame, "Unescaped 0xFF"):
            _decode_inner_frame(malformed)

    def test_literal_checksum_preserves_stuffed_body(self) -> None:
        raw = bytes.fromhex("ffff0a00000000000001ff55a0ff")
        frame, consumed = _decode_inner_frame(raw)
        self.assertEqual(frame.payload, b"\xff\xa0")
        self.assertEqual(consumed, len(raw))

    def test_literal_checksum_keeps_validation_strict(self) -> None:
        for raw, reason in (
            ("ffff0900000000000001f4ff", "checksum_mismatch"),
            ("ffff0900000000000001f5", "truncated_inner"),
            ("ffff0900000000000001f5ff01", "invalid_escape"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidFrame) as caught:
                    _decode_inner_frame(bytes.fromhex(raw))
                self.assertEqual(caught.exception.reason, reason)

    def test_envelope_decoder_handles_fragmented_and_coalesced_packets(
        self,
    ) -> None:
        first = _frame_response(0x02, b"\x6d\x01", sequence=7)
        second = _frame_response(0x06, b"\x6d\x01", sequence=8)
        decoder = _EnvelopeDecoder()

        self.assertEqual(decoder.feed(first[:3]), ())
        decoded = decoder.feed(first[3:] + second)

        self.assertEqual([packet.sequence for packet in decoded], [7, 8])
        self.assertEqual(
            [packet.payload for packet in decoded],
            [
                _inner_frame(0x02, b"\x6d\x01"),
                _inner_frame(0x06, b"\x6d\x01"),
            ],
        )
        self.assertEqual(decoder.buffered_bytes, 0)

    async def _read_snapshot(self, words: list[int]) -> DeviceSnapshot:
        response = _state_response(words)

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            header = await reader.readexactly(80)
            payload_length = int.from_bytes(header[76:80], "big")
            payload = await reader.readexactly(payload_length)
            self.assertEqual(int.from_bytes(header[:4], "big"), 10004)
            self.assertEqual(int.from_bytes(header[72:76], "big"), 0)
            self.assertEqual(payload, _inner_frame(0x01, b"\x4d\x01"))
            for chunk in (response[:3], response[3:79], response[79:]):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]

        return await UwtClient(
            host="127.0.0.1",
            port=port,
            timeout=1,
            device_id="TESTDEVICE",
        ).read_state()

    async def _control_client(
        self,
        handler: Callable[
            [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
        ],
    ) -> UwtClient:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        return UwtClient(
            host="127.0.0.1",
            port=port,
            timeout=1,
            control_timeout=1,
            device_id="TESTDEVICE",
        )

    async def test_idle_drum_does_not_publish_stale_remaining_time(self) -> None:
        words = [0] * 41
        words[0] = 0x6D01
        words[28] = 0x0200

        snapshot = await self._read_snapshot(words)

        self.assertIsNone(snapshot.lower.remaining_minutes)

    async def test_non_idle_drum_publishes_remaining_time(self) -> None:
        words = [0] * 41
        words[0] = 0x6D01
        words[26] = 0x0100
        words[28] = 0x0200

        snapshot = await self._read_snapshot(words)

        self.assertEqual(snapshot.lower.remaining_minutes, 2)

    async def test_decodes_dual_drum_metadata_and_raw_consumption(self) -> None:
        words = [0] * 41
        words[0] = 0x6D01
        words[1] = 0x1400
        words[3] = 0x0E03
        words[7] = 368
        words[8] = 2
        words[9] = 0
        words[10] = 0xF850
        words[11] = 1
        words[12] = 1
        words[13] = 0x262D
        words[24] = 0x1D00
        words[26] = 0x1223
        words[30] = 446

        snapshot = await self._read_snapshot(words)

        self.assertEqual(snapshot.upper.program, "baby_care")
        self.assertEqual(snapshot.upper.phase, "intermittent_rotation")
        self.assertEqual(snapshot.upper.cycle_count, 368)
        self.assertEqual(snapshot.lower.program, "dry")
        self.assertEqual(snapshot.lower.phase, "drying_finished")
        self.assertEqual(snapshot.lower.cycle_count, 446)
        self.assertEqual(snapshot.current_water_raw, 2)
        self.assertEqual(snapshot.total_water_raw, 63568)
        self.assertEqual(snapshot.current_electricity_raw, 1)
        self.assertEqual(snapshot.total_electricity_raw, 75309)

    async def test_timeout_reports_state_stage_after_connecting(self) -> None:
        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.read(4096)
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]

        with self.assertRaisesRegex(
            CannotConnect, "Timed out waiting for a state report"
        ):
            await UwtClient(
                host="127.0.0.1",
                port=port,
                timeout=0.1,
                device_id="TESTDEVICE",
            ).read_state()

    async def test_normal_snapshot_omits_unproven_raw_fields(self) -> None:
        words = [0] * 41
        words[0] = 0x6D01
        words[4] = 0x0100
        words[27] = 0x0100

        snapshot = asdict(await self._read_snapshot(words))

        unproven = {"door_state", "running", "start_flag", "hint_code"}
        self.assertTrue(unproven.isdisjoint(snapshot["upper"]))
        self.assertTrue(unproven.isdisjoint(snapshot["lower"]))

    def test_raw_unproven_fields_exist_only_in_explicit_diagnostics(self) -> None:
        words = [0] * 41
        words[0] = 0x6D01
        words[4] = 0x0100
        words[27] = 0x0100
        frame = _inner_frame(0x06, b"".join(word.to_bytes(2, "big") for word in words))
        observed_at = datetime.now(UTC)

        normal = _inspect_inner(frame, include_raw=False, observed_at=observed_at)
        diagnostic = _inspect_inner(frame, include_raw=True, observed_at=observed_at)

        self.assertNotIn("_diagnostics", normal)
        self.assertNotIn("door_state", normal["state"]["upper"])
        self.assertEqual(
            diagnostic["_diagnostics"]["raw_profile_fields"],
            {
                "upper_door": 1,
                "lower_door": 1,
                "upper_605004": 0,
                "upper_unknown_bit7": 0,
                "lower_60500v": 0,
                "lower_unknown_bit7": 0,
                "upper_hint": 0,
                "lower_hint": 0,
            },
        )

    async def test_start_sends_exact_group_once_and_waits_for_report(
        self,
    ) -> None:
        standby = _standby_words()
        active = _active_spin_words()
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(standby, frame_type=0x02))
            await writer.drain()
            requests.append(await _accept_control_initialization(reader, writer))
            requests.append(await _read_request(reader))
            writer.write(
                _frame_response(0x03, b"\x00\x00", sequence=2) + _state_response(active)
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        result = await client.start_program(
            update_intent(default_intent("upper", "spin"), spin_rpm=400),
            allow_live=True,
            expected=_snapshot(standby),
        )

        self.assertEqual(
            requests,
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
                (1, _inner_frame(0x73, b"")),
                (
                    2,
                    _inner_frame(
                        0x60,
                        bytes.fromhex(
                            "00011c000000000000000190000200000000000000000000"
                        ),
                    ),
                ),
            ],
        )
        self.assertEqual(result.before.upper.phase_value, 0)
        self.assertEqual(result.after.upper.program_value, 0x1C)
        self.assertEqual(result.after.upper.phase_value, 0x0B)
        self.assertTrue(result.after.upper.door_locked)

    async def test_start_rejects_changed_revision_without_control_write(
        self,
    ) -> None:
        expected = _standby_words()
        current = _active_spin_words()
        requests: list[tuple[int, bytes]] = []
        trailing: list[bytes] = []
        connection_closed = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(current, frame_type=0x02))
            await writer.drain()
            trailing.append(await reader.read())
            connection_closed.set()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaises(StateChanged):
            await client.start_program(
                default_intent("upper", "spin"),
                allow_live=True,
                expected=_snapshot(expected),
            )

        await asyncio.wait_for(connection_closed.wait(), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(trailing, [b""])

    async def test_start_rejects_remote_control_off_without_control_write(
        self,
    ) -> None:
        standby = _standby_words(remote_control=False)
        trailing: list[bytes] = []
        connection_closed = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _read_request(reader)
            writer.write(_state_response(standby, frame_type=0x02))
            await writer.drain()
            trailing.append(await reader.read())
            connection_closed.set()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaisesRegex(UnsafeState, "physical Remote Control"):
            await client.start_program(
                default_intent("upper", "spin"),
                allow_live=True,
                expected=_snapshot(standby),
            )

        await asyncio.wait_for(connection_closed.wait(), 1)
        self.assertEqual(trailing, [b""])

    async def test_start_requires_initialized_session_before_control(
        self,
    ) -> None:
        standby = _standby_words()
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(standby, frame_type=0x02))
            await writer.drain()
            requests.append(await _read_request(reader))
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaises(CannotConnect):
            await client.start_program(
                default_intent("upper", "spin"),
                allow_live=True,
                expected=_snapshot(standby),
            )

        self.assertEqual(
            requests,
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
                (1, _inner_frame(0x73, b"")),
            ],
        )

    async def test_start_without_correlated_response_has_unknown_outcome(self) -> None:
        standby = _standby_words()
        active = _active_spin_words()
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(standby, frame_type=0x02))
            await writer.drain()
            requests.append(await _accept_control_initialization(reader, writer))
            requests.append(await _read_request(reader))
            writer.write(_state_response(active))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaisesRegex(OutcomeUnknown, "sent exactly once"):
            await client.start_program(
                default_intent("upper", "spin"),
                allow_live=True,
                expected=_snapshot(standby),
            )

        self.assertEqual(len(requests), 3)

    async def test_start_confirms_report_with_literal_ff_checksum(self) -> None:
        standby = _standby_words()
        active = _cycle_words("upper", program=0x0F, phase=0x05)
        active[14], active[16] = 1, 3
        # Balance a synthetic report to checksum FF without copying device data.
        active[40] = 0x0051
        body = bytes((90, 0, 0, 0, 0, 0, 0, 0x02)) + b"".join(
            word.to_bytes(2, "big") for word in active
        )
        inner = b"\xff\xff" + body + b"\xff"
        response = struct.pack(
            ">II32s32sII", 10005, 0, b"", b"TESTDEVICE", 3, len(inner)
        ) + inner
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(standby))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                writer.write(_frame_response(0x05, b"", sequence=2))
                await writer.drain()
                requests.append(await _read_request(reader))
                # Fragment the checksum boundary across TCP writes.
                writer.write(response[:-1])
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(response[-1:])
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        client = await self._control_client(handle)
        with patch(
            "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
            0.01,
        ):
            result = await client.start_program(
                default_intent("upper", "quick_wash"), allow_live=True
            )
        await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(result.after.upper.program, "quick_wash")
        self.assertTrue(result.after.upper.cycle_active)
        self.assertEqual(requests[3:], [(3, _inner_frame(0x01, b"\x4d\x01"))])
        self.assertEqual(
            sum(
                _decode_inner_frame(frame)[0].frame_type == 0x60
                for _, frame in requests
            ),
            1,
        )

    async def test_start_confirms_late_state_after_an_early_unsuccessful_query(
        self,
    ) -> None:
        standby = _standby_words()
        active = _cycle_words("upper", program=0x0F, phase=0x05)
        active[14], active[16] = 1, 3
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(standby))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                writer.write(_frame_response(0x05, b"", sequence=2))
                await writer.drain()
                requests.append(await _read_request(reader))
                writer.write(
                    _state_response(standby, sequence=3, frame_type=0x02)
                    + _state_response(active)
                )
                await writer.drain()
                requests.append(await _read_request(reader))
                writer.write(_state_response(active, sequence=4, frame_type=0x02))
                await writer.drain()
                await reader.read()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        client = await self._control_client(handle)
        with patch(
            "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
            0.01,
        ):
            result = await client.start_program(
                default_intent("upper", "quick_wash"), allow_live=True
            )
            await asyncio.wait_for(finished.wait(), 1)

        self.assertEqual(result.after.upper.program, "quick_wash")
        self.assertTrue(result.after.upper.cycle_active)
        query = _inner_frame(0x01, b"\x4d\x01")
        self.assertEqual(requests[3:], [(3, query), (4, query)])
        self.assertEqual(
            sum(
                _decode_inner_frame(frame)[0].frame_type == 0x60
                for _, frame in requests
            ),
            1,
        )

    async def test_late_start_report_cannot_override_a_negative_fresh_query(
        self,
    ) -> None:
        standby = _standby_words()
        active = _cycle_words("upper", program=0x0F, phase=0x05)
        active[14], active[16] = 1, 3
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(standby))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                writer.write(_frame_response(0x05, b"", sequence=2))
                await writer.drain()
                requests.append(await _read_request(reader))
                writer.write(
                    _state_response(standby, sequence=3, frame_type=0x02)
                    + _state_response(active)
                )
                await writer.drain()
                requests.append(await _read_request(reader))
                writer.write(_state_response(standby, sequence=4, frame_type=0x02))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        client = await self._control_client(handle)
        with (
            patch(
                "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
                0.01,
            ),
            self.assertRaises(OutcomeUnknown),
        ):
            await client.start_program(
                default_intent("upper", "quick_wash"), allow_live=True
            )
        await asyncio.wait_for(finished.wait(), 1)
        query = _inner_frame(0x01, b"\x4d\x01")
        self.assertEqual(requests[3:], [(3, query), (4, query)])

    async def test_start_succeeds_via_correlated_state_response_without_grouped_ack(
        self,
    ) -> None:
        # A matching type-02 response can satisfy correlation without type 03.
        # The independent active report is still required; this does not claim
        # every grouped write produces type 02 or accept unclassified type 05.
        standby = _standby_words()
        active = _active_spin_words()
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(standby, frame_type=0x02))
            await writer.drain()
            requests.append(await _accept_control_initialization(reader, writer))
            requests.append(await _read_request(reader))
            writer.write(
                _state_response(active, sequence=2, frame_type=0x02)
                + _state_response(active)
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        result = await client.start_program(
            default_intent("upper", "spin"),
            allow_live=True,
            expected=_snapshot(standby),
        )

        self.assertEqual(len(requests), 3)
        self.assertEqual(result.after.upper.program_value, 0x1C)
        self.assertEqual(result.after.upper.phase_value, 0x0B)
        self.assertTrue(result.after.upper.door_locked)

    async def test_cancel_sends_4d13_once_and_proves_standby_not_drain(
        self,
    ) -> None:
        active = _active_spin_words()
        phase_zero_locked = _cancelled_spin_words(locked=True)
        unlocked = _cancelled_spin_words(locked=False)
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(active, frame_type=0x02))
            await writer.drain()
            requests.append(await _accept_control_initialization(reader, writer))
            requests.append(await _read_request(reader))
            writer.write(
                _state_response(active, sequence=2, frame_type=0x02)
                + _state_response(phase_zero_locked)
                + _state_response(unlocked)
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        result = await client.control_drum(
            "upper", "cancel", allow_live=True, expected=_snapshot(active)
        )

        self.assertEqual(
            requests,
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
                (1, _inner_frame(0x73, b"")),
                (2, _inner_frame(0x01, b"\x4d\x13")),
            ],
        )
        self.assertEqual(result.before.upper.phase_value, 0x0B)
        self.assertEqual(result.after.upper.phase_value, 0)
        self.assertTrue(result.after.upper.door_locked)
        self.assertTrue(result.after.remote_control)

    async def test_cancel_rejects_unknown_phase_without_control_write(
        self,
    ) -> None:
        active = _active_spin_words(phase=0x15)
        trailing: list[bytes] = []
        connection_closed = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _read_request(reader)
            writer.write(_state_response(active, frame_type=0x02))
            await writer.drain()
            trailing.append(await reader.read())
            connection_closed.set()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaises(UnsafeState):
            await client.control_drum(
                "upper", "cancel", allow_live=True, expected=_snapshot(active)
            )

        await asyncio.wait_for(connection_closed.wait(), 1)
        self.assertEqual(trailing, [b""])

    async def test_cancel_requires_correlated_response_before_success(
        self,
    ) -> None:
        active = _active_spin_words()
        unlocked = _cancelled_spin_words(locked=False)

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _read_request(reader)
            writer.write(_state_response(active, frame_type=0x02))
            await writer.drain()
            await _accept_control_initialization(reader, writer)
            await _read_request(reader)
            writer.write(_state_response(unlocked))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with self.assertRaisesRegex(OutcomeUnknown, "sent exactly once"):
            await client.control_drum(
                "upper", "cancel", allow_live=True, expected=_snapshot(active)
            )

    async def test_cancel_keeps_session_alive_until_delayed_standby(self) -> None:
        active = _active_spin_words()
        unlocked = _cancelled_spin_words(locked=False)
        heartbeats: list[bytes] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _read_request(reader)
            writer.write(_state_response(active, frame_type=0x02))
            await writer.drain()
            await _accept_control_initialization(reader, writer)
            await _read_request(reader)
            writer.write(_state_response(active, sequence=2, frame_type=0x02))
            await writer.drain()

            header = await reader.readexactly(16)
            payload_length = int.from_bytes(header[12:16], "big")
            payload = await reader.readexactly(payload_length)
            heartbeats.append(header + payload)
            counter = int.from_bytes(header[8:12], "big")
            heartbeat_reply_payload = payload + b"\0\0\0\0"
            writer.write(
                struct.pack(
                    ">IIII",
                    24051,
                    0,
                    counter,
                    len(heartbeat_reply_payload),
                )
                + heartbeat_reply_payload
                + _state_response(unlocked)
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        with patch(
            "custom_components.uplus_reborn.protocol._protocol._HEARTBEAT_INTERVAL",
            0.01,
        ):
            result = await client.control_drum(
                "upper", "cancel", allow_live=True, expected=_snapshot(active)
            )

        self.assertEqual(
            heartbeats,
            [_build_heartbeat_request("TESTDEVICE", 3)],
        )
        self.assertEqual(result.after.upper.phase_value, 0)
        self.assertFalse(result.after.upper.door_locked)

    async def test_observer_preserves_actual_fragmented_and_coalesced_stream(
        self,
    ) -> None:
        unmatched = _state_response(_active_spin_words(), sequence=19)
        selected = _state_response(_standby_words(), frame_type=0x02)
        trailing = _state_response(_active_spin_words())
        incoming = unmatched + selected + trailing
        expected_tx = (
            struct.pack(">II32s32sII", 10004, 0, b"", b"TESTDEVICE", 0, 13)
            + bytes.fromhex("ffff0a000000000000014d0159")
        )
        traffic: list[list[bytes]] = []
        for enabled in (False, True):
            with self.subTest(capture=enabled):
                requests: list[bytes] = []
                finished = asyncio.Event()
                first_read = asyncio.Event()

                class FragmentObserver(_RecordingObserver):
                    def __init__(
                        self, first_read: asyncio.Event, *, active: bool
                    ) -> None:
                        super().__init__(active=active)
                        self.first_read = first_read

                    def raw(
                        self, direction: str, data: bytes, *, stage: str
                    ) -> None:
                        super().raw(direction, data, stage=stage)
                        if direction == "rx":
                            self.first_read.set()

                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    *,
                    requests: list[bytes] = requests,
                    finished: asyncio.Event = finished,
                    first_read: asyncio.Event = first_read,
                    enabled: bool = enabled,
                ) -> None:
                    try:
                        header = await reader.readexactly(80)
                        requests.append(
                            header
                            + await reader.readexactly(
                                int.from_bytes(header[76:80], "big")
                            )
                        )
                        writer.write(incoming[:7])
                        await writer.drain()
                        if enabled:
                            await asyncio.wait_for(first_read.wait(), 1)
                        else:
                            await asyncio.sleep(0.01)
                        writer.write(incoming[7:])
                        await writer.drain()
                        requests.append(await reader.read())
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        finished.set()

                observer = FragmentObserver(first_read, active=enabled)
                client = replace(await self._control_client(handle), observer=observer)
                with self.assertLogs(
                    "custom_components.uplus_reborn.protocol._protocol", level="DEBUG"
                ) as logs:
                    state = await client.read_state()
                await asyncio.wait_for(finished.wait(), 1)
                ordinary_logs = "\n".join(logs.output)
                self.assertNotIn("TESTDEVICE", ordinary_logs)
                self.assertNotIn(incoming.hex(), ordinary_logs)
                self.assertNotIn(expected_tx.hex(), ordinary_logs)
                self.assertEqual(state.upper.phase_value, 0)
                traffic.append(requests)
                if not enabled:
                    self.assertEqual(observer.events, [])
                    self.assertEqual(observer.raw_chunks, [])
                    continue
                self.assertEqual(
                    observer.raw_chunks[0], ("tx", expected_tx, "write_submitted")
                )
                received = [
                    data
                    for direction, data, stage in observer.raw_chunks
                    if direction == "rx" and stage == "read"
                ]
                self.assertEqual(received, [incoming[:7], incoming[7:]])
                self.assertTrue(all(len(data) <= 4096 for data in received))
                states = [
                    context
                    for event, context in observer.events
                    if event == "state_observed"
                ]
                self.assertEqual(
                    [context["decision"] for context in states],
                    ["uncorrelated", "correlated"],
                )
        self.assertEqual(traffic, [[expected_tx, b""], [expected_tx, b""]])

    async def test_observer_retains_rejected_bytes_before_strict_failure(self) -> None:
        header = _frame_response(0x05, b"")[:76]
        cases = (
            ("checksum_mismatch", bytes.fromhex("ffff08000000000000050c")),
            ("invalid_escape", bytes.fromhex("ffff0a00000000000001ff0160")),
            ("invalid_inner_size", bytes.fromhex("ffff0700000000000005")),
        )
        packets = [
            (reason, header + len(inner).to_bytes(4, "big") + inner)
            for reason, inner in cases
        ]
        packets.extend(
            (
                ("partial_envelope", _state_response(_standby_words())[:-4]),
                ("invalid_frame", header + (4097).to_bytes(4, "big")),
            )
        )
        for reason, packet in packets:
            with self.subTest(reason=reason):
                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    *,
                    packet: bytes = packet,
                ) -> None:
                    await _read_request(reader)
                    writer.write(packet)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                observer = _RecordingObserver()
                client = replace(await self._control_client(handle), observer=observer)
                with self.assertRaises(InvalidFrame):
                    await client.read_state()
                self.assertEqual(
                    b"".join(
                        data
                        for direction, data, _ in observer.raw_chunks
                        if direction == "rx"
                    ),
                    packet,
                )
                failures = [
                    context for event, context in observer.events if event == "failed"
                ]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["cause"], reason)
                self.assertLess(
                    observer.order.index("raw_rx"), observer.order.index("failed")
                )
                self.assertFalse(
                    any(event == "state_observed" for event, _ in observer.events)
                )

    async def test_initialization_observes_both_sides_of_acceptance_before_refusal(
        self,
    ) -> None:
        for after_acceptance in (False, True):
            with self.subTest(after_acceptance=after_acceptance):
                requests: list[tuple[int, bytes]] = []
                finished = asyncio.Event()
                changed = _standby_words(remote_control=False)

                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    *,
                    requests: list[tuple[int, bytes]] = requests,
                    finished: asyncio.Event = finished,
                    changed: list[int] = changed,
                    after_acceptance: bool = after_acceptance,
                ) -> None:
                    requests.append(await _read_request(reader))
                    writer.write(_state_response(_standby_words()))
                    await writer.drain()
                    initialization = await _read_request(reader)
                    requests.append(initialization)
                    accepted = _frame_response(
                        0x74, b"", sequence=initialization[0]
                    )
                    report = _state_response(changed)
                    writer.write(
                        accepted + report if after_acceptance else report + accepted
                    )
                    await writer.drain()
                    self.assertEqual(await reader.read(), b"")
                    writer.close()
                    await writer.wait_closed()
                    finished.set()

                observer = _RecordingObserver()
                client = replace(await self._control_client(handle), observer=observer)
                with self.assertRaises(StateChanged):
                    await client.start_program(
                        default_intent("upper", "spin"), allow_live=True
                    )
                await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(len(requests), 2)
                states = [
                    context
                    for event, context in observer.events
                    if event == "state_observed"
                ]
                self.assertEqual(
                    [context["stage"] for context in states],
                    ["query", "initialization"],
                )
                self.assertEqual(
                    states[-1]["decision"],
                    "pre_dispatch_pending_observation"
                    if after_acceptance
                    else "pre_dispatch_observation",
                )
                self.assertFalse(states[-1]["decoded_snapshot"].remote_control)
                self.assertTrue(
                    any(
                        event == "failed" and context["cause"] == "state_changed"
                        for event, context in observer.events
                    )
                )

    async def test_query_observes_reports_in_order_without_changing_selected_reply(
        self,
    ) -> None:
        active = _active_spin_words()
        finished = _standby_words(program=0x1C)
        finished[3] = 0x1303
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            final_query = requests[0]
            writer.write(
                _state_response(active, sequence=17)
                + _state_response(
                    finished,
                    sequence=final_query[0],
                    frame_type=0x02,
                )
            )
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        observer = _RecordingObserver()
        client = replace(await self._control_client(handle), observer=observer)
        final = await client.read_state()

        self.assertEqual(
            requests,
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
            ],
        )
        observations = [
            context
            for event, context in observer.events
            if event == "state_observed"
        ]
        self.assertEqual(
            [
                context["decoded_snapshot"].upper.phase_value
                for context in observations
            ],
            [0x0B, 0x13],
        )
        self.assertEqual(
            [context["decision"] for context in observations],
            ["uncorrelated", "correlated"],
        )
        self.assertEqual(final.upper.phase_value, 0x13)
        self.assertFalse(final.upper.door_locked)

    async def _exercise_transition(
        self,
        before: list[int],
        after: list[int],
        invoke: Callable[[UwtClient], Awaitable[ControlResult]],
        *,
        interim: list[list[int]] | None = None,
    ) -> tuple[ControlResult, list[tuple[int, bytes]]]:
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(before, frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                writer.write(_frame_response(0x03, b"\x00\x00", sequence=2))
                for words in interim or ():
                    writer.write(_state_response(words))
                writer.write(_state_response(after))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        result = await invoke(await self._control_client(handle))
        await asyncio.wait_for(finished.wait(), 1)
        return result, requests

    async def test_captured_speed_coordinate_is_not_configuration_proof(self) -> None:
        # Minimal C11f/C10-A contract: requested 400/1200, reported word 19 zero.
        # Zero is not interpreted as missing or accepted as an exact-speed echo.
        for speed in (400, 1200):
            with self.subTest(speed=speed):
                observed = _active_spin_words()
                observed[19] = 0
                result, _ = await self._exercise_transition(
                    _standby_words(),
                    observed,
                    lambda client, speed=speed: client.start_program(
                        update_intent(default_intent("upper", "spin"), spin_rpm=speed),
                        allow_live=True,
                    ),
                )
                self.assertEqual(result.after.upper.program, "spin")
                self.assertTrue(result.after.upper.cycle_active)
                self.assertEqual(result.unconfirmed_settings, ("spin_rpm",))

    async def test_timed_drying_proves_mode_without_a_main_wash_echo(self) -> None:
        # Live Dry/30 reports active phase 05 and zero main-wash coordinates.
        after = _cycle_words("lower", program=0x1D, phase=0x05)
        after[24], after[28] = 0x1D07, 30 << 8
        after[31], after[32], after[33] = 0, 0, 0
        wrong_mode = after.copy()
        wrong_mode[24] = 0x1D06
        result, requests = await self._exercise_transition(
            _standby_words(),
            after,
            lambda client: client.start_program(
                update_intent(default_intent("lower", "dry"), drying_mode="30_min"),
                allow_live=True,
            ),
            interim=[wrong_mode],
        )
        self.assertEqual(result.after.lower.reported_settings.drying_value, 7)
        self.assertEqual(result.after.lower.reported_settings.wash_minutes_encoded, 0)
        self.assertTrue(result.after.lower.cycle_active)
        command = _decode_inner_frame(requests[2][1])[0]
        self.assertEqual(struct.unpack(">12H", command.payload)[4], 30)

    async def test_refresh_proves_preserved_drying_not_ui_label(self) -> None:
        before = _standby_words()
        before[24] = 0x0106
        after = _cycle_words("lower", program=0x0C, phase=0x0C)
        after[24], after[32], after[33] = 0x0C06, 0, 0
        result, requests = await self._exercise_transition(
            before,
            after,
            lambda client: client.start_program(
                default_intent("lower", "refresh"), allow_live=True
            ),
        )
        self.assertEqual(
            requests[2],
            (
                2,
                _inner_frame(
                    0x60,
                    bytes.fromhex("00020c000600000000000000000000000000000000000000"),
                ),
            ),
        )
        self.assertEqual(result.after.lower.reported_settings.drying_value, 6)
        self.assertIn("drying_mode", result.unconfirmed_settings)

    async def test_smart_fabric_stain_proves_fixed_source_recipe(self) -> None:
        # A1 + W30 clamps to 15 minutes/cold, then adds the source five minutes.
        after = _cycle_words("upper", program=0x13, phase=0x05)
        after[2], after[14], after[15], after[16] = 0x0200, 2, 2, 20
        result, requests = await self._exercise_transition(
            _standby_words(),
            after,
            lambda client: client.start_program(
                fabric_intent("upper", "A1", ("W30",)), allow_live=True
            ),
        )
        self.assertEqual(
            requests[2],
            (
                2,
                _inner_frame(
                    0x60,
                    bytes.fromhex("000113000002000000140000020200000000000000000000"),
                ),
            ),
        )
        self.assertTrue(result.after.upper.reported_settings.smart)
        self.assertEqual(result.after.upper.reported_settings.wash_minutes_encoded, 20)
        self.assertEqual(result.unconfirmed_settings, ())

    async def test_both_drums_pause_resume_cancel_and_finished_reset(self) -> None:
        for drum in ("upper", "lower"):
            program_word, phase_word = (1, 3) if drum == "upper" else (24, 26)
            for action, opcode in (
                ("pause", 0x4D05),
                ("resume", 0x4D04),
                ("cancel", 0x4D13),
                ("reset", 0x4D13),
            ):
                with self.subTest(drum=drum, action=action):
                    before = _cycle_words(
                        drum,
                        program=0x0F,
                        phase=0x13 if action == "reset" else 0x05,
                        active=action != "resume",
                    )
                    after = before.copy()
                    after[phase_word] = (
                        0x0523
                        if action == "pause"
                        else 0x05A3
                        if action == "resume"
                        else 0x0023
                    )
                    other_program, other_phase = (24, 26) if drum == "upper" else (1, 3)
                    before[other_program] = after[other_program] = 0x1C00
                    before[other_phase] = after[other_phase] = 0x0BA3
                    result, requests = await self._exercise_transition(
                        before,
                        after,
                        lambda client, drum=drum, action=action, before=before: (
                            client.control_drum(
                                drum,
                                action,
                                allow_live=True,
                                expected=_snapshot(before),
                            )
                        ),
                    )
                    self.assertEqual(
                        requests[2],
                        (
                            2,
                            _inner_frame(
                                0x01,
                                (opcode + (0x80 if drum == "lower" else 0)).to_bytes(
                                    2, "big"
                                ),
                            ),
                        ),
                    )
                    target = (
                        result.after.upper if drum == "upper" else result.after.lower
                    )
                    self.assertEqual(target.program_value, after[program_word] >> 8)
                    self.assertEqual(target.phase_value, after[phase_word] >> 8)
                    self.assertEqual(target.cycle_active, action == "resume")
                    self.assertEqual(len(requests), 3)

    async def test_lower_group_start_preserves_shared_buzzer_and_builder_context(
        self,
    ) -> None:
        before = _cycle_words("upper")
        before[24] = 0x0100
        before[2] = 0x0100
        before[32], before[35], before[40] = 0x0300, 0x0200, 0x1234
        before[38], before[39] = 0x0500, 0x0607
        after = before.copy()
        after[24], after[26], after[28] = 0x1C00, 0x0BA3, 0x0600
        after[32] = 0x0302
        result, requests = await self._exercise_transition(
            before,
            after,
            lambda client: client.start_program(
                update_intent(default_intent("lower", "spin"), spin_rpm=400),
                allow_live=True,
                expected=_snapshot(before),
            ),
        )
        self.assertEqual(
            requests[2],
            (
                2,
                _inner_frame(
                    0x60,
                    bytes.fromhex(
                        "0002 1c00 0001 0003 0000 0190 0002 0000 0000 1234 0506 0700"
                    ),
                ),
            ),
        )
        self.assertEqual(result.after.lower.program, "spin")
        self.assertEqual(result.after.upper.phase_value, 0x0B)
        self.assertFalse(result.after.buzzer_enabled)

    async def test_ordinary_stain_selection_and_clearing_replace_retained_enum(
        self,
    ) -> None:
        base = update_intent(
            default_intent("lower", "mix"),
            temperature_c=30,
            wash_minutes=50,
            rinses=8,
            spin_rpm=800,
        )
        for stains, stain_word, setting_word in (
            (("wine",), 0x0300, 0x3219),
            ((), 0, 0x1E41),
        ):
            with self.subTest(stains=stains):
                before = _standby_words()
                before[24], before[35] = 0x0500, 0x0600
                after = _cycle_words("lower", program=5, phase=5)
                after[31], after[33], after[35] = 8, setting_word, stain_word
                intent = update_intent(base, stains=stains)
                result, requests = await self._exercise_transition(
                    before,
                    after,
                    lambda client, intent=intent: client.start_program(
                        intent, allow_live=True
                    ),
                )
                expected = struct.pack(
                    ">12H",
                    2,
                    0x0500,
                    0,
                    0,
                    setting_word,
                    800,
                    0x0802,
                    0,
                    stain_word,
                    0,
                    0,
                    0,
                )
                self.assertEqual(requests[2], (2, _inner_frame(0x60, expected)))
                self.assertEqual(result.before.lower.reported_settings.stain_value, 6)
                self.assertEqual(
                    result.after.lower.reported_settings.stain_value, stain_word >> 8
                )
                self.assertEqual(result.after.lower.program, "mix")
                self.assertEqual(len(requests), 3)

    async def test_delayed_start_requires_reservation_phase_not_generic_activity(
        self,
    ) -> None:
        intent = default_intent("upper", "quick_wash")
        choices = program_delay_capabilities(
            "upper", "quick_wash"
        ).available_half_hour_steps
        intent = update_intent(
            intent, delay_half_hour_step=next(step for step in choices if step)
        )
        before = _standby_words(program=0x1C)
        wrong = _cycle_words("upper", program=0x0F, phase=0x05)
        delayed = _cycle_words("upper", program=0x0F, phase=0x0F)
        wrong[14], wrong[15], wrong[16] = 1, 2, 3
        delayed[14], delayed[15], delayed[16] = 1, 2, 3
        delayed[2] |= 0x04
        result, _ = await self._exercise_transition(
            before,
            delayed,
            lambda client: client.start_program(intent, allow_live=True),
            interim=[wrong],
        )
        self.assertEqual(result.after.upper.phase_value, 0x0F)
        self.assertTrue(result.after.upper.delay_active)
        self.assertEqual(
            result.unconfirmed_settings, ("spin_rpm", "delay_half_hour_step")
        )

    async def test_pause_resume_delayed_cycle(self) -> None:
        for action in ("pause", "resume"):
            before = _cycle_words("lower", phase=0x0F, active=action == "pause")
            before[25] |= 0x04
            after = before.copy()
            after[26] ^= 0x80
            result, _ = await self._exercise_transition(
                before,
                after,
                lambda client, action=action: client.control_drum(
                    "lower", action, allow_live=True
                ),
            )
            self.assertEqual(result.after.lower.phase_value, 0x0F)
            self.assertEqual(result.after.lower.cycle_active, action == "resume")

    async def test_unrelated_progress_during_initialization_does_not_abort_start(
        self,
    ) -> None:
        before = _cycle_words("lower")
        current = before.copy()
        current[28], current[10] = 0x0500, 2
        active = current.copy()
        active[1], active[3], active[5] = 0x1C00, 0x0BA3, 0x0600
        active[15] = 2
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            requests.append(await _read_request(reader))
            writer.write(_state_response(current))
            await writer.drain()
            requests.append(await _read_request(reader))
            current[28] = 0x0400
            writer.write(
                _state_response(current) + _frame_response(0x74, b"", sequence=1)
            )
            await writer.drain()
            requests.append(await _read_request(reader))
            writer.write(
                _frame_response(0x03, b"", sequence=2) + _state_response(active)
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client = await self._control_client(handle)
        result = await client.start_program(
            default_intent("upper", "spin"), allow_live=True, expected=_snapshot(before)
        )
        self.assertEqual(result.before.lower.remaining_minutes, 4)
        self.assertEqual(result.after.upper.phase_value, 0x0B)
        self.assertEqual(len(requests), 3)

    async def test_target_transition_buffered_after_init_prevents_dispatch(
        self,
    ) -> None:
        before = _standby_words()
        trailing: list[bytes] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _read_request(reader)
            writer.write(_state_response(before))
            await writer.drain()
            await _read_request(reader)
            writer.write(
                _frame_response(0x74, b"", sequence=1)
                + _state_response(_active_spin_words())
            )
            await writer.drain()
            trailing.append(await reader.read())
            writer.close()
            await writer.wait_closed()
            finished.set()

        client = await self._control_client(handle)
        with self.assertRaises(StateChanged):
            await client.start_program(default_intent("upper", "spin"), allow_live=True)
        await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(trailing, [b""])

    async def test_global_options_confirm_their_own_state_with_active_drums(
        self,
    ) -> None:
        for option, off, on, word, mask, inverted in (
            ("power", 0x4D03, 0x4D02, 3, 0x01, False),
            ("buzzer", 0x4D2F, 0x4D2E, 2, 0x100, True),
            ("child_lock", 0x4D07, 0x4D06, 2, 0x08, False),
        ):
            for enabled in (False, True):
                with self.subTest(option=option, enabled=enabled):
                    before = _cycle_words("upper")
                    before[24], before[26] = 0x0100, 0x05A3
                    if enabled == inverted:
                        before[word] |= mask
                    else:
                        before[word] &= ~mask
                    after = before.copy()
                    after[word] ^= mask
                    result, requests = await self._exercise_transition(
                        before,
                        after,
                        lambda client, option=option, enabled=enabled, before=before: (
                            client.set_appliance_option(
                                option,
                                enabled,
                                allow_live=True,
                                expected=_snapshot(before),
                            )
                        ),
                    )
                    self.assertEqual(
                        requests[2],
                        (
                            2,
                            _inner_frame(
                                0x01, (on if enabled else off).to_bytes(2, "big")
                            ),
                        ),
                    )
                    attribute = "buzzer_enabled" if option == "buzzer" else option
                    self.assertEqual(getattr(result.after, attribute), enabled)

    async def test_child_unlock_clears_coupled_child_and_local_locks(self) -> None:
        before = _standby_words()
        before[2] |= 0x08
        before[3] |= 0x08
        after = before.copy()
        after[2] &= ~0x08
        after[3] &= ~0x08
        result, requests = await self._exercise_transition(
            before,
            after,
            lambda client: client.set_appliance_option(
                "child_lock", False, allow_live=True, expected=_snapshot(before)
            ),
        )
        self.assertEqual(requests[2], (2, _inner_frame(0x01, b"\x4d\x07")))
        self.assertFalse(result.after.child_lock)
        self.assertFalse(result.after.local_lock)
        self.assertTrue(result.after.remote_control)

    async def test_child_unlock_exception_preserves_other_control_preconditions(
        self,
    ) -> None:
        for option, enabled, flags, status, reason in (
            ("child_lock", False, 0, 0x0B, "local_lock"),
            ("child_lock", True, 8, 0x0B, "local_lock"),
            ("buzzer", False, 8, 0x0B, "local_lock"),
            ("child_lock", False, 8, 0x09, "remote_control_off"),
            ("child_lock", False, 8, 0x0A, "power_off"),
            ("power", True, 8, 0x0A, "local_lock"),
        ):
            with self.subTest(
                option=option, enabled=enabled, flags=flags, status=status
            ):
                before = _standby_words()
                before[2], before[3] = flags, status
                trailing: list[bytes] = []
                finished = asyncio.Event()

                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    *,
                    before: list[int] = before,
                    trailing: list[bytes] = trailing,
                    finished: asyncio.Event = finished,
                ) -> None:
                    await _read_request(reader)
                    writer.write(_state_response(before))
                    await writer.drain()
                    trailing.append(await reader.read())
                    writer.close()
                    await writer.wait_closed()
                    finished.set()

                client = await self._control_client(handle)
                with self.assertRaises(UnsafeState) as raised:
                    await client.set_appliance_option(option, enabled, allow_live=True)
                await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(trailing, [b""])

    async def test_opt_out_refuses_every_command_before_network(self) -> None:
        client = UwtClient("127.0.0.1", "TESTDEVICE")
        with patch("asyncio.open_connection", side_effect=AssertionError("network")):
            for invoke in (
                lambda: client.start_program(
                    default_intent("upper", "spin"), allow_live=False
                ),
                lambda: client.control_drum("lower", "cancel", allow_live=False),
                lambda: client.set_appliance_option("power", False, allow_live=False),
            ):
                with self.assertRaises(UnsafeState):
                    await invoke()

    def test_source_child_lock_is_not_local_lock_or_physical_door(self) -> None:
        before = _standby_words()
        before[2] = 0x0108
        state = _snapshot(before)
        self.assertTrue(state.child_lock)
        self.assertFalse(state.local_lock)
        self.assertFalse(state.buzzer_enabled)
        with self.assertRaises(UnsafeState):
            validate_program_start(state, default_intent("upper", "spin"))
        before[2] = 0
        before[3] |= 0x20
        validate_program_start(_snapshot(before), default_intent("upper", "spin"))

    def test_finished_and_unknown_phases_are_not_paused_cycles(self) -> None:
        for phase in (0, 0x12, 0x13, 0x15):
            state = _snapshot(_cycle_words("upper", phase=phase, active=False))
            with self.subTest(phase=phase), self.assertRaises(UnsafeState):
                validate_drum_control(state, "upper", "resume")
        for phase in (0x0B, 0x0E, 0x0F):
            state = _snapshot(_cycle_words("upper", phase=phase))
            with self.subTest(phase=phase), self.assertRaises(UnsafeState):
                validate_drum_control(state, "upper", "reset")
