"""Unsigned consumption widths and independent profile coordinates."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from custom_components.uplus_reborn.protocol import DeviceSnapshot
from custom_components.uplus_reborn.protocol._protocol import (
    _build_inner_frame,
    _decode_inner_frame,
    _decode_state,
    _state_words,
)

from .test_protocol import _standby_words


class ConsumptionContractTest(unittest.TestCase):
    def _decode(self, words: list[int]) -> DeviceSnapshot:
        # Boundary words contain FF bytes, so use the existing stuffed-frame builder.
        encoded = _build_inner_frame(
            0x06, b"".join(word.to_bytes(2, "big") for word in words)
        )
        frame, consumed = _decode_inner_frame(encoded)
        self.assertEqual(consumed, len(encoded))
        return _decode_state(_state_words(frame), datetime(2026, 1, 1, tzinfo=UTC))

    def test_current_fields_preserve_unsigned_sixteen_bit_boundaries(self) -> None:
        for word, field in ((8, "current_water_raw"), (11, "current_electricity_raw")):
            for value in (0x7FFF, 0x8000, 0xFFFF):
                with self.subTest(word=word, value=value):
                    words = _standby_words()
                    words[word] = value
                    snapshot = self._decode(words)
                    self.assertEqual(getattr(snapshot, field), value)
                    other = (
                        snapshot.current_electricity_raw
                        if word == 8
                        else snapshot.current_water_raw
                    )
                    self.assertEqual(other, 0)
                    self.assertEqual(snapshot.total_water_raw, 0)
                    self.assertEqual(snapshot.total_electricity_raw, 0)

    def test_totals_preserve_unsigned_high_word_first_boundaries(self) -> None:
        for word, field in ((9, "total_water_raw"), (12, "total_electricity_raw")):
            for value in (0xFFFF, 0x10000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
                with self.subTest(word=word, value=value):
                    words = _standby_words()
                    words[word] = value >> 16
                    words[word + 1] = value & 0xFFFF
                    snapshot = self._decode(words)
                    self.assertEqual(getattr(snapshot, field), value)
                    other = (
                        snapshot.total_electricity_raw
                        if word == 9
                        else snapshot.total_water_raw
                    )
                    self.assertEqual(other, 0)
                    self.assertEqual(snapshot.current_water_raw, 0)
                    self.assertEqual(snapshot.current_electricity_raw, 0)

    def test_consumption_does_not_overlap_cycle_counts_or_adjacent_words(self) -> None:
        words = _standby_words()
        words[8:14] = [0x8001, 0x1234, 0xABCD, 0x8002, 0x4567, 0x89AB]
        for word in (7, 14, 30):
            words[word] = 0xFFFF
        snapshot = self._decode(words)

        self.assertEqual(snapshot.current_water_raw, 0x8001)
        self.assertEqual(snapshot.total_water_raw, 0x1234ABCD)
        self.assertEqual(snapshot.current_electricity_raw, 0x8002)
        self.assertEqual(snapshot.total_electricity_raw, 0x456789AB)
        self.assertEqual(snapshot.upper.cycle_count, 0xFFFF)
        self.assertEqual(snapshot.lower.cycle_count, 0xFFFF)
