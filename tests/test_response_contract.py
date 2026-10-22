"""One-shot control outcomes require action-specific authoritative state."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import pytest

from custom_components.uplus_reborn.protocol import (
    InvalidFrame,
    OutcomeUnknown,
    UwtClient,
)
from custom_components.uplus_reborn.protocol._commands import (
    default_intent,
    update_intent,
)
from custom_components.uplus_reborn.protocol._protocol import _decode_inner_frame

from .test_protocol import (
    _accept_control_initialization,
    _active_spin_words,
    _cycle_words,
    _frame_response,
    _inner_frame,
    _read_request,
    _snapshot,
    _standby_words,
    _state_response,
)

_EMPTY_RAW_05 = bytes.fromhex("ffff08000000000000050d")


@pytest.mark.usefixtures("loopback_socket")
class ResponseContractTest(unittest.IsolatedAsyncioTestCase):
    def test_exact_empty_raw_05_decodes_without_reclassification(self) -> None:
        frame, consumed = _decode_inner_frame(_EMPTY_RAW_05)

        self.assertEqual(consumed, len(_EMPTY_RAW_05))
        self.assertEqual(frame.frame_type, 0x05)
        self.assertEqual(frame.payload, b"")
        self.assertEqual(frame.flags, 0)
        self.assertEqual(frame.reserved, bytes(5))

    def test_empty_raw_05_with_bad_checksum_is_rejected(self) -> None:
        with self.assertRaises(InvalidFrame):
            _decode_inner_frame(_EMPTY_RAW_05[:-1] + b"\x0c")

    def test_completed_phase_preserves_independent_reported_lock(self) -> None:
        words = _active_spin_words(phase=0x13)
        words[5] = 0
        locked = _snapshot(words)
        words[3] &= ~0x20
        unlocked = _snapshot(words)

        self.assertEqual(locked.upper.phase_value, 0x13)
        self.assertTrue(locked.upper.door_locked)
        self.assertEqual(locked.upper.remaining_minutes, 0)
        self.assertEqual(unlocked.upper.phase_value, 0x13)
        self.assertFalse(unlocked.upper.door_locked)
        self.assertEqual(unlocked.upper.remaining_minutes, 0)

    async def test_matching_raw_05_and_active_report_remain_unknown_without_retry(
        self,
    ) -> None:
        standby = _standby_words()
        requests: list[tuple[int, bytes]] = []
        trailing: list[bytes] = []
        connections = 0
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal connections
            connections += 1
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(standby, frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                sequence, request = await _read_request(reader)
                requests.append((sequence, request))
                response = _frame_response(0x05, b"", sequence=sequence)
                self.assertEqual(response[80:], _EMPTY_RAW_05)
                writer.write(response + _state_response(_active_spin_words()))
                await writer.drain()
                # Keep the connection open: the client must time out, not retry.
                trailing.append(await reader.read())
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                port=server.sockets[0].getsockname()[1],
                timeout=1,
                control_timeout=0.1,
                device_id="TESTDEVICE",
            )
            with self.assertRaises(OutcomeUnknown):
                await client.start_program(
                    update_intent(default_intent("upper", "spin"), spin_rpm=400),
                    allow_live=True,
                    expected=_snapshot(standby),
                )
            await asyncio.wait_for(finished.wait(), 1)

        self.assertEqual(connections, 1)
        self.assertEqual(trailing, [b""])
        self.assertEqual(
            requests,
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
                (1, _inner_frame(0x73, b"")),
                (
                    2,
                    bytes.fromhex(
                        "ffff200000000000006000011c000000000000000190"
                        "00020000000000000000000030"
                    ),
                ),
            ],
        )

    async def test_raw_05_can_be_resolved_only_by_matching_fresh_requested_state(
        self,
    ) -> None:
        for variant in (
            "active",
            "paused",
            "completed",
            "locked",
            "wrong_program",
            "wrong_sequence",
        ):
            with self.subTest(variant=variant):
                standby = _standby_words()
                requests: list[tuple[int, bytes]] = []
                finished = asyncio.Event()
                fresh = _active_spin_words()
                if variant == "paused":
                    fresh[3] &= ~0x80
                elif variant == "completed":
                    fresh[3] = 0x13A3
                elif variant == "locked":
                    fresh[3] = 0x0023
                elif variant == "wrong_program":
                    fresh[1] = 0x0F00

                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    *,
                    requests=requests,
                    standby=standby,
                    fresh=fresh,
                    variant=variant,
                    finished=finished,
                ) -> None:
                    try:
                        requests.append(await _read_request(reader))
                        writer.write(_state_response(standby, frame_type=0x02))
                        await writer.drain()
                        requests.append(
                            await _accept_control_initialization(reader, writer)
                        )
                        requests.append(await _read_request(reader))
                        writer.write(
                            _frame_response(0x05, b"", sequence=2)
                            + _state_response(_active_spin_words())
                        )
                        await writer.drain()
                        query = await _read_request(reader)
                        requests.append(query)
                        writer.write(
                            _state_response(
                                fresh,
                                sequence=0 if variant == "wrong_sequence" else query[0],
                                frame_type=0x02,
                            )
                        )
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        finished.set()

                server = await asyncio.start_server(handle, "127.0.0.1", 0)
                async with server:
                    client = UwtClient(
                        "127.0.0.1",
                        "TESTDEVICE",
                        port=server.sockets[0].getsockname()[1],
                        control_timeout=1,
                    )
                    with patch(
                        "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
                        0.01,
                    ):
                        if variant == "active":
                            result = await client.start_program(
                                default_intent("upper", "spin"), allow_live=True
                            )
                            self.assertTrue(result.after.upper.cycle_active)
                        else:
                            with self.assertRaises(OutcomeUnknown):
                                await client.start_program(
                                    default_intent("upper", "spin"), allow_live=True
                                )
                    await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(len(requests), 4)
                self.assertEqual(requests[-1], (3, _inner_frame(0x01, b"\x4d\x01")))
                self.assertEqual(sum(packet[9] == 0x60 for _, packet in requests), 1)

    async def test_tub_clean_start_rechecks_state_after_pending_query(self) -> None:
        """A pending correlated status must not stop one-shot Start confirmation."""
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()
        pending = _cycle_words("lower", program=13, phase=0)
        pending[28] = 0
        pending[31], pending[33], pending[34] = 1, 0x5A4B, 0x0040
        running = pending.copy()
        running[26], running[28] = 0x05A3, 90 << 8

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(_standby_words(), frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                control = await _read_request(reader)
                requests.append(control)
                writer.write(_frame_response(0x05, b"", sequence=control[0]))
                await writer.drain()
                first_query = await _read_request(reader)
                requests.append(first_query)
                writer.write(
                    _state_response(pending, sequence=first_query[0], frame_type=0x02)
                    + _state_response(pending, sequence=first_query[0])
                    + _state_response(pending)
                )
                await writer.drain()
                # No later unsolicited report: only a fresh query can prove Start.
                second_query = await _read_request(reader)
                requests.append(second_query)
                writer.write(
                    _state_response(running, sequence=second_query[0], frame_type=0x02)
                )
                await writer.drain()
            except asyncio.IncompleteReadError:
                # The broken client closes here when confirmation stalls.
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                "TESTDEVICE",
                port=server.sockets[0].getsockname()[1],
                control_timeout=0.3,
            )
            with patch(
                "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
                0.01,
            ):
                try:
                    result = await client.start_program(
                        update_intent(
                            default_intent("lower", "drum_clean"),
                            enabled_functions=("night",),
                        ),
                        allow_live=True,
                    )
                finally:
                    await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(result.after.lower.phase, "washing")
        self.assertEqual(result.after.lower.remaining_minutes, 90)
        self.assertEqual(
            [(sequence, packet) for sequence, packet in requests if packet[9] == 1],
            [
                (0, _inner_frame(0x01, b"\x4d\x01")),
                (3, _inner_frame(0x01, b"\x4d\x01")),
                (4, _inner_frame(0x01, b"\x4d\x01")),
            ],
        )
        self.assertEqual(sum(packet[9] == 0x60 for _, packet in requests), 1)

    async def test_pending_queries_keep_deadline_and_reject_unsolicited_proof(
        self,
    ) -> None:
        """Repeated reads cannot turn pending state or an uncorrelated report into ACK."""
        requests: list[tuple[int, bytes]] = []
        trailing: list[bytes] = []
        finished = asyncio.Event()
        pending = _cycle_words("lower", program=13, phase=0)
        pending[28] = 0
        pending[31], pending[33], pending[34] = 1, 0x5A4B, 0x0040
        running = pending.copy()
        running[26], running[28] = 0x05A3, 90 << 8

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(_standby_words(), frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                control = await _read_request(reader)
                requests.append(control)
                writer.write(_frame_response(0x05, b"", sequence=control[0]))
                await writer.drain()
                for _ in range(2):
                    query = await _read_request(reader)
                    requests.append(query)
                    writer.write(
                        _state_response(pending, sequence=query[0], frame_type=0x02)
                        + _state_response(running)
                    )
                    await writer.drain()
                # A third query receives no answer: the original deadline applies.
                requests.append(await _read_request(reader))
                trailing.append(await reader.read())
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                "TESTDEVICE",
                port=server.sockets[0].getsockname()[1],
                control_timeout=0.3,
            )
            with (
                patch(
                    "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
                    0.01,
                ),
                self.assertRaises(OutcomeUnknown),
            ):
                await asyncio.wait_for(
                    client.start_program(
                        update_intent(
                            default_intent("lower", "drum_clean"),
                            enabled_functions=("night",),
                        ),
                        allow_live=True,
                    ),
                    1,
                )
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(trailing, [b""])
        self.assertEqual(
            [sequence for sequence, packet in requests if packet[9] == 1],
            [0, 3, 4, 5],
        )
        self.assertEqual(sum(packet[9] == 0x60 for _, packet in requests), 1)

    async def test_control_proof_is_processed_during_followup_query(self) -> None:
        """A delayed control ACK and independent report need no query reply."""
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()
        pending = _cycle_words("lower", program=13, phase=0)
        pending[28] = 0
        pending[31], pending[33], pending[34] = 1, 0x5A4B, 0x0040
        running = pending.copy()
        running[26], running[28] = 0x05A3, 90 << 8

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(_standby_words(), frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                control = await _read_request(reader)
                requests.append(control)
                writer.write(_frame_response(0x05, b"", sequence=control[0]))
                await writer.drain()
                query = await _read_request(reader)
                requests.append(query)
                writer.write(
                    _state_response(pending, sequence=query[0], frame_type=0x02)
                )
                await writer.drain()
                requests.append(await _read_request(reader))
                # Query has no reply; the normal control proof arrives instead.
                writer.write(
                    _frame_response(0x03, b"", sequence=control[0])
                    + _state_response(running)
                )
                await writer.drain()
                await reader.read()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                "TESTDEVICE",
                port=server.sockets[0].getsockname()[1],
                control_timeout=0.3,
            )
            with patch(
                "custom_components.uplus_reborn.protocol._protocol._CONFIRMATION_QUERY_DELAY",
                0.01,
            ):
                try:
                    result = await client.start_program(
                        update_intent(
                            default_intent("lower", "drum_clean"),
                            enabled_functions=("night",),
                        ),
                        allow_live=True,
                    )
                finally:
                    await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(result.after.lower.phase, "washing")
        self.assertEqual(
            [sequence for sequence, packet in requests if packet[9] == 1],
            [0, 3, 4],
        )
        self.assertEqual(sum(packet[9] == 0x60 for _, packet in requests), 1)

    async def test_power_off_disconnect_is_not_success_or_a_wake_retry(self) -> None:
        requests: list[tuple[int, bytes]] = []
        finished = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(_standby_words(), frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                "TESTDEVICE",
                port=server.sockets[0].getsockname()[1],
                control_timeout=1,
            )
            with self.assertRaises(OutcomeUnknown):
                await client.set_appliance_option("power", False, allow_live=True)
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(requests[2], (2, _inner_frame(0x01, b"\x4d\x03")))
        self.assertEqual(len(requests), 3)

    async def test_cancellation_after_dispatch_preserves_uncertain_boundary(
        self,
    ) -> None:
        dispatched = asyncio.Event()
        finished = asyncio.Event()
        requests: list[tuple[int, bytes]] = []

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                requests.append(await _read_request(reader))
                writer.write(_state_response(_standby_words(), frame_type=0x02))
                await writer.drain()
                requests.append(await _accept_control_initialization(reader, writer))
                requests.append(await _read_request(reader))
                dispatched.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            client = UwtClient(
                "127.0.0.1",
                "TESTDEVICE",
                port=server.sockets[0].getsockname()[1],
                control_timeout=1,
            )
            task = asyncio.create_task(
                client.start_program(default_intent("upper", "spin"), allow_live=True)
            )
            await asyncio.wait_for(dispatched.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as raised:
                await task
            self.assertTrue(raised.exception.control_dispatched)
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(len(requests), 3)

    async def test_start_requires_all_source_observable_settings(self) -> None:
        # Upper: 10 + heating 10 + soak 10 + intensive 15; lower: 60 + heating 15.
        # Initial wash-stage proof is distinct from remaining total cycle time.
        fixtures = (
            (
                "upper",
                "casual",
                ("high_water", "intensive", "soak"),
                0x15,
                0x1E2D,
                0x0025,
                0,
                "00011500000000001e2d03e8020200250000000000000000",
            ),
            (
                "lower",
                "cotton",
                ("high_water", "eco", "night"),
                0x01,
                0x1E4B,
                0x0049,
                6,
                "00020100060000001e4b05dc020200490000000000000000",
            ),
        )
        for drum, program, functions, value, wash, flags, drying, group in fixtures:
            intent = update_intent(
                default_intent(drum, program),
                enabled_functions=functions,
                **({"drying_mode": "60_min"} if drum == "lower" else {}),
            )
            for mismatch in (
                "none",
                "reserved_option_bits",
                "temperature",
                "wash",
                "rinses",
                "spin_time",
                "options",
                "smart",
                "stain",
                *(("drying",) if drum == "lower" else ()),
            ):
                with self.subTest(drum=drum, mismatch=mismatch):
                    words = _cycle_words(drum, program=value, phase=5)
                    settings_word = 16 if drum == "upper" else 33
                    rinse_word = 14 if drum == "upper" else 31
                    spin_word = 15 if drum == "upper" else 32
                    flags_word = 17 if drum == "upper" else 34
                    smart_word = 2 if drum == "upper" else 25
                    words[settings_word] = wash
                    words[rinse_word], words[spin_word], words[flags_word] = 2, 2, flags
                    if drum == "lower":
                        words[24] |= drying
                    if mismatch == "temperature":
                        words[settings_word] ^= 0x0100
                    elif mismatch == "wash":
                        words[settings_word] ^= 0x01
                    elif mismatch == "rinses":
                        words[rinse_word] = 3
                    elif mismatch == "spin_time":
                        words[spin_word] = 3
                    elif mismatch == "options":
                        words[flags_word] ^= 0x01
                    elif mismatch == "smart":
                        words[smart_word] |= 0x0200
                    elif mismatch == "drying":
                        words[24] ^= 0x01
                    elif mismatch == "reserved_option_bits":
                        words[flags_word] |= 0x0090
                    elif mismatch == "stain":
                        words[18 if drum == "upper" else 35] = 0x0100
                    requests = []
                    finished = asyncio.Event()

                    async def handle(
                        reader,
                        writer,
                        *,
                        requests=requests,
                        words=words,
                        finished=finished,
                    ) -> None:
                        try:
                            requests.append(await _read_request(reader))
                            writer.write(
                                _state_response(_standby_words(), frame_type=0x02)
                            )
                            await writer.drain()
                            requests.append(
                                await _accept_control_initialization(reader, writer)
                            )
                            requests.append(await _read_request(reader))
                            writer.write(
                                _frame_response(0x03, b"", sequence=2)
                                + _state_response(words)
                            )
                            await writer.drain()
                        finally:
                            writer.close()
                            await writer.wait_closed()
                            finished.set()

                    server = await asyncio.start_server(handle, "127.0.0.1", 0)
                    async with server:
                        client = UwtClient(
                            "127.0.0.1",
                            "TESTDEVICE",
                            port=server.sockets[0].getsockname()[1],
                            control_timeout=1,
                        )
                        if mismatch in ("none", "reserved_option_bits"):
                            result = await client.start_program(intent, allow_live=True)
                            target = (
                                result.after.upper
                                if drum == "upper"
                                else result.after.lower
                            )
                            self.assertEqual(target.phase_value, 5)
                            self.assertEqual(target.remaining_minutes, 6)
                            self.assertEqual(result.unconfirmed_settings, ("spin_rpm",))
                        else:
                            with self.assertRaises(OutcomeUnknown):
                                await client.start_program(intent, allow_live=True)
                        await asyncio.wait_for(finished.wait(), 1)
                    self.assertEqual(
                        requests[2], (2, _inner_frame(0x60, bytes.fromhex(group)))
                    )
                    self.assertEqual(len(requests), 3)
