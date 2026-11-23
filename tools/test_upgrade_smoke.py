"""Standalone unit tests for `tools/upgrade_smoke.py` policy helpers.

Run with: python -m unittest discover -s tools -p "test_*.py" -v
"""

from __future__ import annotations

import copy
import unittest

from upgrade_smoke import parse_version, phase_exercise_options, verify_upgrade_registry


def _sample_registry(*, pref_disable_new_entities: bool = True) -> dict:
    return {
        "entry": {
            "id": "entry-1",
            "unique_id": "020000000001",
            "title": "Retained appliance entry",
            "data": {"host": "127.0.0.1", "device_id": "020000000001"},
            "options": {"enable_verified_controls": True, "poll_interval": 30},
            "pref_disable_new_entities": pref_disable_new_entities,
        },
        "entities": {
            "sensor:020000000001_water": {
                "id": "ent-water",
                "entity_id": "sensor.release_fixture_water",
                "device_id": "dev-1",
                "name": "Retained water history",
                "icon": "mdi:water-check",
                "area_id": "area-1",
                "disabled_by": None,
                "hidden_by": None,
                "options": {},
            },
            "sensor:020000000001_upper_phase": {
                "id": "ent-phase",
                "entity_id": "sensor.uplus_upper_phase",
                "device_id": "dev-1",
                "name": None,
                "icon": None,
                "area_id": None,
                "disabled_by": None,
                "hidden_by": None,
                "options": {},
            },
        },
        "devices": {
            "dev-1": {
                "identifiers": [["uplus_reborn", "020000000001"]],
                "connections": [],
                "name_by_user": "Retained appliance name",
                "area_id": "area-1",
            },
        },
        "sensor_contracts": {
            "020000000001_water": {
                "unit_of_measurement": "L",
                "device_class": "volume",
                "state_class": "total_increasing",
            },
            "020000000001_upper_phase": {
                "unit_of_measurement": None,
                "device_class": "enum",
                "state_class": None,
            },
        },
    }


def _new_entity(
    entity_id: str,
    unique_id: str,
    *,
    device_id: str = "dev-1",
    disabled_by: str | None = "integration",
    name: str | None = None,
) -> dict:
    return {
        "id": f"id-{unique_id}",
        "entity_id": entity_id,
        "device_id": device_id,
        "name": name,
        "icon": None,
        "area_id": None,
        "disabled_by": disabled_by,
        "hidden_by": None,
        "options": {},
    }


def _previous(version: str, *, pref_disable_new_entities: bool = True) -> dict:
    return {
        "integration_version": version,
        "upgrade": {"registry": _sample_registry(pref_disable_new_entities=pref_disable_new_entities)},
    }


class ParseVersionTests(unittest.TestCase):
    def test_accepts_strict_semver_triples(self) -> None:
        self.assertEqual(parse_version("0.0.0"), (0, 0, 0))
        self.assertEqual(parse_version("1.0.2"), (1, 0, 2))
        self.assertEqual(parse_version("1.10.23"), (1, 10, 23))

    def test_rejects_invalid_or_non_string_versions(self) -> None:
        for invalid in ("v1.0.2", "1.1", "01.1.0", "1.1.0-rc1", "", None, 110):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_version(invalid)  # type: ignore[arg-type]


class PhaseExerciseOptionsTests(unittest.TestCase):
    def test_selects_compatible_smoke_expectations_across_versions(self) -> None:
        self.assertEqual(
            phase_exercise_options("1.0.2", None),
            {"fault_entities": "absent", "profile_management": False},
        )
        self.assertEqual(
            phase_exercise_options("1.1.0", None),
            {"fault_entities": "enabled", "profile_management": True},
        )
        self.assertEqual(
            phase_exercise_options("1.1.0", _previous("1.1.0", pref_disable_new_entities=True)),
            {"fault_entities": "enabled", "profile_management": True},
        )
        self.assertEqual(
            phase_exercise_options("1.1.0", _previous("1.0.2", pref_disable_new_entities=True)),
            {"fault_entities": "disabled", "profile_management": True},
        )
        self.assertEqual(
            phase_exercise_options("1.1.0", _previous("1.0.2", pref_disable_new_entities=False)),
            {"fault_entities": "enabled", "profile_management": True},
        )


class VerifyUpgradeRegistryTests(unittest.TestCase):
    def test_same_version_restart_requires_exact_registry_match(self) -> None:
        old = _sample_registry()
        self.assertEqual(
            verify_upgrade_registry(old, copy.deepcopy(old), old_version="1.1.0", new_version="1.1.0"),
            [],
        )
        changed = copy.deepcopy(old)
        changed["entities"]["sensor:020000000001_upper_fault_code"] = _new_entity(
            "sensor.uplus_upper_fault_code", "id-fault", disabled_by="integration",
        )
        with self.assertRaisesRegex(RuntimeError, "Restart changed"):
            verify_upgrade_registry(old, changed, old_version="1.1.0", new_version="1.1.0")

    def test_rejects_downgrade(self) -> None:
        old = _sample_registry()
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            verify_upgrade_registry(old, copy.deepcopy(old), old_version="1.1.0", new_version="1.0.2")

    def test_accepts_v1_0_2_to_v1_1_0_additive_fault_entities_when_disabled(self) -> None:
        old = _sample_registry(pref_disable_new_entities=True)
        new = copy.deepcopy(old)
        fault_entities = {
            "binary_sensor:020000000001_upper_fault": _new_entity(
                "binary_sensor.uplus_upper_fault", "upper_fault",
            ),
            "binary_sensor:020000000001_lower_fault": _new_entity(
                "binary_sensor.uplus_lower_fault", "lower_fault",
            ),
            "sensor:020000000001_upper_fault_code": _new_entity(
                "sensor.uplus_upper_fault_code", "upper_fault_code",
            ),
            "sensor:020000000001_lower_fault_code": _new_entity(
                "sensor.uplus_lower_fault_code", "lower_fault_code",
            ),
        }
        new["entities"].update(fault_entities)
        added = verify_upgrade_registry(old, new, old_version="1.0.2", new_version="1.1.0")
        self.assertEqual(added, sorted(fault_entities))

    def test_rejects_removed_or_altered_existing_entity_and_entry_or_device(self) -> None:
        old = _sample_registry()
        removed = copy.deepcopy(old)
        del removed["entities"]["sensor:020000000001_upper_phase"]
        with self.assertRaisesRegex(RuntimeError, "removed existing entities"):
            verify_upgrade_registry(old, removed, old_version="1.0.2", new_version="1.1.0")

        for field, value in (
            ("entity_id", "sensor.renamed"),
            ("name", "Altered"),
            ("disabled_by", "user"),
            ("options", {"custom": True}),
        ):
            altered = copy.deepcopy(old)
            altered["entities"]["sensor:020000000001_water"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeError, "changed existing entity"),
            ):
                verify_upgrade_registry(old, altered, old_version="1.0.2", new_version="1.1.0")

        entry_changed = copy.deepcopy(old)
        entry_changed["entry"]["options"]["poll_interval"] = 15
        with self.assertRaisesRegex(RuntimeError, "config entry"):
            verify_upgrade_registry(old, entry_changed, old_version="1.0.2", new_version="1.1.0")

        device_changed = copy.deepcopy(old)
        device_changed["devices"]["dev-1"]["name_by_user"] = "Other"
        with self.assertRaisesRegex(RuntimeError, "device identity"):
            verify_upgrade_registry(old, device_changed, old_version="1.0.2", new_version="1.1.0")

    def test_rejects_altered_existing_sensor_contract(self) -> None:
        old = _sample_registry()
        changed = copy.deepcopy(old)
        changed["sensor_contracts"]["020000000001_water"]["unit_of_measurement"] = "mL"
        with self.assertRaisesRegex(RuntimeError, "sensor unit/class contract"):
            verify_upgrade_registry(old, changed, old_version="1.0.2", new_version="1.1.0")

    def test_rejects_unknown_device_enabled_new_entity_or_contract_under_pref_disable(self) -> None:
        old = _sample_registry(pref_disable_new_entities=True)

        wrong_device = copy.deepcopy(old)
        wrong_device["entities"]["binary_sensor:020000000001_upper_fault"] = _new_entity(
            "binary_sensor.uplus_upper_fault", "upper_fault", device_id="dev-other",
        )
        with self.assertRaisesRegex(RuntimeError, "unknown device"):
            verify_upgrade_registry(old, wrong_device, old_version="1.0.2", new_version="1.1.0")

        not_disabled = copy.deepcopy(old)
        not_disabled["entities"]["binary_sensor:020000000001_upper_fault"] = _new_entity(
            "binary_sensor.uplus_upper_fault", "upper_fault", disabled_by=None,
        )
        with self.assertRaisesRegex(RuntimeError, "pref_disable_new_entities"):
            verify_upgrade_registry(old, not_disabled, old_version="1.0.2", new_version="1.1.0")

        with_user_name = copy.deepcopy(old)
        with_user_name["entities"]["binary_sensor:020000000001_upper_fault"] = _new_entity(
            "binary_sensor.uplus_upper_fault", "upper_fault", name="Unexpected",
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected user metadata"):
            verify_upgrade_registry(old, with_user_name, old_version="1.0.2", new_version="1.1.0")

        live_contract = copy.deepcopy(old)
        live_contract["entities"]["sensor:020000000001_upper_fault_code"] = _new_entity(
            "sensor.uplus_upper_fault_code", "upper_fault_code",
        )
        live_contract["sensor_contracts"]["020000000001_upper_fault_code"] = {
            "unit_of_measurement": None,
            "device_class": "enum",
            "state_class": None,
        }
        with self.assertRaisesRegex(RuntimeError, "live contracts"):
            verify_upgrade_registry(old, live_contract, old_version="1.0.2", new_version="1.1.0")

    def test_accepts_enabled_additive_sensor_contract_when_pref_disabled_is_false_and_rejects_orphan(self) -> None:
        old = _sample_registry(pref_disable_new_entities=False)
        new = copy.deepcopy(old)
        new["entities"]["sensor:020000000001_upper_fault_code"] = _new_entity(
            "sensor.uplus_upper_fault_code", "upper_fault_code", disabled_by=None,
        )
        new["sensor_contracts"]["020000000001_upper_fault_code"] = {
            "unit_of_measurement": None,
            "device_class": "enum",
            "state_class": None,
        }
        added = verify_upgrade_registry(old, new, old_version="1.0.2", new_version="1.1.0")
        self.assertEqual(added, ["sensor:020000000001_upper_fault_code"])

        orphan = copy.deepcopy(old)
        orphan["sensor_contracts"]["020000000001_orphan"] = {
            "unit_of_measurement": None,
            "device_class": "enum",
            "state_class": None,
        }
        with self.assertRaisesRegex(RuntimeError, "unexpected entity"):
            verify_upgrade_registry(old, orphan, old_version="1.0.2", new_version="1.1.0")


if __name__ == "__main__":
    unittest.main()
