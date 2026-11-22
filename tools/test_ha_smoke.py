"""Standalone unit tests for `tools/ha_smoke.py` helpers.

Run with: python -m unittest discover -s tools -p test_ha_smoke.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import ha_smoke


class _LegacyStore:
    def __init__(self, path: str) -> None:
        self.path = path


class _FileIO:
    def __init__(self, path: str) -> None:
        self.path = path


class _ModernStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._io = _FileIO(path)


class _ReadOnlyFileIO:
    def __init__(self, path: str) -> None:
        self._path = path

    @property
    def path(self) -> str:
        return self._path


class _ReadOnlyIoModernStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._io = _ReadOnlyFileIO(path)


class ImpossibleStoreTargetTests(unittest.TestCase):
    def test_redirects_and_restores_legacy_store_on_exit_and_error(self) -> None:
        storage_path = Path("/tmp/.storage/uplus_reborn.profiles.entry1")
        expected_impossible = str(storage_path / "impossible-commit")
        store = _LegacyStore(str(storage_path))

        with ha_smoke._impossible_store_target(store, storage_path):
            self.assertEqual(store.path, expected_impossible)
        self.assertEqual(store.path, str(storage_path))

        with (
            self.assertRaises(OSError),
            ha_smoke._impossible_store_target(store, storage_path),
        ):
            self.assertEqual(store.path, expected_impossible)
            raise OSError("boom")
        self.assertEqual(store.path, str(storage_path))

    def test_redirects_and_restores_core_2026_11_io_adapter(self) -> None:
        storage_path = Path("/tmp/.storage/uplus_reborn.profiles.entry2")
        expected_impossible = str(storage_path / "impossible-commit")
        store = _ModernStore(str(storage_path))

        with ha_smoke._impossible_store_target(store, storage_path):
            self.assertEqual(store.path, expected_impossible)
            self.assertEqual(store._io.path, expected_impossible)
        self.assertEqual(store.path, str(storage_path))
        self.assertEqual(store._io.path, str(storage_path))

        with (
            self.assertRaises(OSError),
            ha_smoke._impossible_store_target(store, storage_path),
        ):
            self.assertEqual(store._io.path, expected_impossible)
            raise OSError("boom")
        self.assertEqual(store.path, str(storage_path))
        self.assertEqual(store._io.path, str(storage_path))

    def test_partial_setup_failure_restores_earlier_target_and_propagates(self) -> None:
        storage_path = Path("/tmp/.storage/uplus_reborn.profiles.entry3")
        store = _ReadOnlyIoModernStore(str(storage_path))

        with (
            self.assertRaises(AttributeError),
            ha_smoke._impossible_store_target(store, storage_path),
        ):
            self.fail("Context manager body should not execute after partial setup failure")
        self.assertEqual(store.path, str(storage_path))
        self.assertEqual(store._io.path, str(storage_path))


if __name__ == "__main__":
    unittest.main()
