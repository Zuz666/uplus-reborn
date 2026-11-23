#!/usr/bin/env python3
"""Verify an integration upgrade across fresh Core processes with persistent data.

Run in the core-current Docker image. Both artifacts are trusted committed-byte
bundles. All appliance traffic is synthetic loopback; no live credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import ha_smoke as smoke
from delivery import (
    append_github_output,
    extract_artifact,
    load_artifact,
    verify_installed,
)

VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
FAULT_ENTITIES_MIN_VERSION = (1, 1, 0)
PROFILE_MANAGEMENT_MIN_VERSION = (1, 1, 0)
UPGRADE_RELEVANT_FILES = frozenset({
    "tools/ha_smoke.py",
    "tools/upgrade_smoke.py",
    "tools/Dockerfile",
    "tools/delivery.py",
    "tools/release.py",
    "hacs.json",
})


def is_upgrade_relevant_path(path: str) -> bool:
    """Return True if changing `path` requires running the PR upgrade guard."""
    return path.startswith("custom_components/") or path in UPGRADE_RELEVANT_FILES


def select_pr_upgrade_applicability(repo: Path, environment: dict[str, str]) -> dict:
    """Conservatively decide whether the PR upgrade guard applies; fails closed."""
    if environment.get("GITHUB_EVENT_NAME") != "pull_request":
        return {"schema": 1, "run_upgrade": True, "reason": "non_pull_request", "matched_paths": []}
    try:
        event = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"schema": 1, "run_upgrade": True, "reason": "unavailable_comparison", "matched_paths": []}
    if not (isinstance(base, str) and COMMIT_RE.fullmatch(base) and isinstance(head, str) and COMMIT_RE.fullmatch(head)):
        return {"schema": 1, "run_upgrade": True, "reason": "unavailable_comparison", "matched_paths": []}
    merge_base = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), "merge-base", base, head],
        capture_output=True, text=True, check=False,
    )
    comparison = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not COMMIT_RE.fullmatch(comparison):
        return {"schema": 1, "run_upgrade": True, "reason": "incomplete_comparison", "matched_paths": []}
    paths: set[str] = set()
    for left, right in ((comparison, head), (base, "HEAD")):
        diff = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), "diff", "--no-ext-diff", "--no-textconv",
             "--no-renames", "--name-only", "-z", left, right, "--"],
            capture_output=True, check=False,
        )
        if diff.returncode != 0:
            return {"schema": 1, "run_upgrade": True, "reason": "incomplete_comparison", "matched_paths": []}
        for raw in diff.stdout.split(b"\0"):
            if raw:
                try:
                    paths.add(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    return {"schema": 1, "run_upgrade": True, "reason": "unknown_changes", "matched_paths": []}
    if not paths:
        return {"schema": 1, "run_upgrade": True, "reason": "unknown_changes", "matched_paths": []}
    matched = sorted(path for path in paths if is_upgrade_relevant_path(path))
    return {
        "schema": 1,
        "run_upgrade": bool(matched),
        "reason": "relevant_changes" if matched else "unrelated_changes",
        "matched_paths": matched,
    }


def parse_version(raw: str) -> tuple[int, int, int]:
    """Parse a strict X.Y.Z release version string into a numeric tuple."""
    if not isinstance(raw, str) or not VERSION_RE.fullmatch(raw):
        raise ValueError(f"Expected X.Y.Z release version, got {raw!r}")
    major, minor, patch = map(int, raw.split("."))
    return major, minor, patch


def phase_exercise_options(installed_version: str, previous: dict | None) -> dict[str, object]:
    """Select Core smoke expectations compatible with baseline and upgraded versions."""
    current = parse_version(installed_version)
    profile_management = current >= PROFILE_MANAGEMENT_MIN_VERSION
    if current < FAULT_ENTITIES_MIN_VERSION:
        fault_entities = "absent"
    elif previous is not None:
        previous_version = parse_version(previous["integration_version"])
        disable_new = bool(previous["upgrade"]["registry"]["entry"]["pref_disable_new_entities"])
        fault_entities = (
            "disabled"
            if previous_version < FAULT_ENTITIES_MIN_VERSION and disable_new
            else "enabled"
        )
    else:
        fault_entities = "enabled"
    return {
        "fault_entities": fault_entities,
        "profile_management": profile_management,
    }


def verify_upgrade_registry(
    old_registry: dict,
    new_registry: dict,
    *,
    old_version: str,
    new_version: str,
    allow_additive: bool = False,
) -> list[str]:
    """Verify preserved identities/preferences/contracts across restarts or upgrades."""
    old_ver = parse_version(old_version)
    new_ver = parse_version(new_version)
    smoke._require(new_ver >= old_ver, f"Unexpected downgrade from {old_version} to {new_version}")
    if new_ver == old_ver and not allow_additive:
        smoke._require(
            new_registry == old_registry,
            "Restart changed identity, preferences or sensor units/classes",
        )
        return []
    smoke._require(
        new_registry["entry"] == old_registry["entry"],
        "Upgrade changed config entry identity or preferences",
    )
    smoke._require(
        new_registry["devices"] == old_registry["devices"],
        "Upgrade changed device identity or user metadata",
    )
    old_entities = old_registry["entities"]
    new_entities = new_registry["entities"]
    removed = sorted(set(old_entities) - set(new_entities))
    smoke._require(not removed, f"Upgrade removed existing entities: {removed}")
    for key, old_row in old_entities.items():
        smoke._require(
            new_entities[key] == old_row,
            f"Upgrade changed existing entity identity or preferences: {key}",
        )
    added = sorted(set(new_entities) - set(old_entities))
    disable_new = bool(old_registry["entry"]["pref_disable_new_entities"])
    known_devices = set(old_registry["devices"])
    for key in added:
        row = new_entities[key]
        smoke._require(
            row["device_id"] in known_devices,
            f"Upgrade attached new entity to unknown device: {key}",
        )
        if disable_new:
            smoke._require(
                row["disabled_by"] == "integration",
                f"Upgrade did not respect pref_disable_new_entities for new entity: {key}",
            )
            smoke._require(
                row["hidden_by"] is None
                and row["name"] is None
                and row["icon"] is None
                and row["area_id"] is None,
                f"New disabled entity carried unexpected user metadata: {key}",
            )
    old_contracts = old_registry["sensor_contracts"]
    new_contracts = new_registry["sensor_contracts"]
    for uid, contract in old_contracts.items():
        smoke._require(
            new_contracts.get(uid) == contract,
            f"Upgrade changed existing sensor unit/class contract: {uid}",
        )
    extra_contracts = sorted(set(new_contracts) - set(old_contracts))
    if disable_new:
        smoke._require(
            not extra_contracts,
            f"Disabled new sensors unexpectedly published live contracts: {extra_contracts}",
        )
    else:
        added_set = set(added)
        for uid in extra_contracts:
            smoke._require(
                f"sensor:{uid}" in added_set,
                f"Upgrade published sensor contract for unexpected entity: {uid}",
            )
    return added


def _registry_snapshot(hass, entry) -> dict:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    result = {
        "entry": {
            "id": entry.entry_id, "unique_id": entry.unique_id, "title": entry.title,
            "data": {key: value for key, value in entry.data.items() if key != "port"},
            "options": dict(entry.options),
            "pref_disable_new_entities": entry.pref_disable_new_entities,
        },
        "entities": {
            f"{row.domain}:{row.unique_id}": {
                "id": row.id, "entity_id": row.entity_id, "device_id": row.device_id,
                "name": row.name, "icon": row.icon, "area_id": row.area_id,
                "disabled_by": row.disabled_by, "hidden_by": row.hidden_by,
                "options": dict(row.options),
            }
            for row in entities
        },
        "devices": {
            device.id: {
                "identifiers": sorted(device.identifiers), "connections": sorted(device.connections),
                "name_by_user": device.name_by_user, "area_id": device.area_id,
            }
            for device in devices
        },
        "sensor_contracts": {},
    }
    smoke._require(len(result["entities"]) == len(entities), "Registry snapshot omitted an entity identity")
    for row in entities:
        if row.domain == "sensor" and (state := hass.states.get(row.entity_id)) is not None:
            result["sensor_contracts"][row.unique_id] = {
                key: state.attributes.get(key)
                for key in ("unit_of_measurement", "device_class", "state_class")
            }
    return json.loads(json.dumps(result, default=dict))


async def _checkpoint(
    hass,
    entry,
    peer,
    result: dict,
    previous: dict | None,
    installed_version: str,
    *,
    allow_additive: bool = False,
) -> None:
    from homeassistant.components import recorder
    from homeassistant.components.recorder import history, statistics
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util.unit_conversion import VolumeConverter

    instance = recorder.get_instance(hass)
    await instance.async_block_till_done()
    registry = er.async_get(hass)
    if previous is None:
        area = ar.async_get(hass).async_create("Release fixture area")
        device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
        dr.async_get(hass).async_update_device(
            device.id, name_by_user="Retained appliance name", area_id=area.id,
        )
        water = next(
            row.entity_id for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            if row.domain == "sensor" and (state := hass.states.get(row.entity_id)) is not None
            and state.attributes.get("unit_of_measurement") == "L"
            and state.attributes.get("state_class") == "total_increasing"
        )
        water = registry.async_update_entity(
            water, new_entity_id="sensor.release_fixture_water",
            name="Retained water history", icon="mdi:water-check", area_id=area.id,
        ).entity_id
        hass.config_entries.async_update_entry(
            entry, title="Retained appliance entry", pref_disable_new_entities=True,
            options={**entry.options, "poll_interval": 30},
        )
        await hass.async_block_till_done()
        since = datetime.now(UTC) - timedelta(seconds=1)
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        state = hass.states.get(water)
        smoke._require(state is not None, "Renamed water entity disappeared")
        # Seed prior user data through Core's APIs, not direct database writes.
        hass.states.async_set(water, "9", dict(state.attributes))
        statistics.async_import_statistics(hass, {
            "statistic_id": water, "source": "recorder", "name": "Retained water history",
            "unit_class": VolumeConverter.UNIT_CLASS, "unit_of_measurement": "L",
            "mean_type": statistics.StatisticMeanType.NONE, "has_sum": True,
        }, [{"start": start, "state": 9.0, "sum": 9.0}])
        peer.words = smoke._words("running")
        before_queries = len(peer.queries)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        smoke._require(len(peer.queries) > before_queries, "Upgrade fixture did not perform a real TCP refresh")
    else:
        water = previous["upgrade"]["water_entity_id"]
        since = datetime.fromisoformat(previous["upgrade"]["history_since"])
        start = datetime.fromisoformat(previous["upgrade"]["statistics_start"])
    await instance.async_block_till_done()
    records = await hass.async_add_executor_job(partial(
        history.get_significant_states, hass, since, entity_ids=[water],
    ))
    recorded = [
        [state.state, state.last_updated.isoformat(), state.attributes.get("unit_of_measurement")]
        for state in records.get(water, [])
    ]
    hourly = await hass.async_add_executor_job(
        statistics.statistics_during_period, hass, start, start + timedelta(hours=1),
        {water}, "hour", None, {"sum", "state"},
    )
    metadata = await hass.async_add_executor_job(partial(
        statistics.get_metadata, hass, statistic_ids={water},
    ))
    smoke._require(water in metadata, "Statistics metadata was lost")
    stat_meta = metadata[water][1]
    evidence = {
        "water_entity_id": water, "history_since": since.isoformat(),
        "statistics_start": start.isoformat(), "history": recorded,
        "statistics": hourly,
        "statistic_identity": {key: stat_meta[key] for key in (
            "statistic_id", "source", "unit_class", "unit_of_measurement", "mean_type", "has_sum",
        )},
        "registry": _registry_snapshot(hass, entry),
    }
    if previous is None:
        smoke._require(any(row[0] == "9" and row[2] == "L" for row in recorded), "Historical litre state was not recorded")
        smoke._require(any(row.get("sum") == 9.0 for row in hourly.get(water, [])), "Historical statistics were not recorded")
    else:
        old = previous["upgrade"]
        evidence["added_entities"] = verify_upgrade_registry(
            old["registry"],
            evidence["registry"],
            old_version=previous["integration_version"],
            new_version=installed_version,
            allow_additive=allow_additive,
        )
        smoke._require(all(row in recorded for row in old["history"]), "Upgrade lost or rescaled historical states")
        smoke._require(hourly == old["statistics"], "Upgrade changed historical consumption statistics")
        smoke._require(evidence["statistic_identity"] == old["statistic_identity"], "Upgrade changed statistic identity or units")
        result["upgrade_preservation_verified"] = True
    result["upgrade"] = evidence


def _phase(root: Path, output: Path, previous_path: Path | None, *, allow_additive: bool = False) -> int:
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path else None
    result = {
        "observations": [], "success": False,
        "python_version": platform.python_version(), "platform": platform.platform(),
    }
    if previous:
        result.update(saved_profiles=previous["saved_profiles"], profile_entry_id=previous["profile_entry_id"])
    guard = smoke.LoopbackGuard()
    sys.addaudithook(guard.audit)
    sys.path.insert(0, str(root))
    logging.basicConfig(level=logging.WARNING)
    try:
        installed_version = json.loads((root / smoke.PACKAGE / "manifest.json").read_text(encoding="utf-8"))["version"]
        result["integration_version"] = installed_version
        exercise_options = phase_exercise_options(installed_version, previous)

        async def probe(hass, entry, peer):
            await _checkpoint(
                hass, entry, peer, result, previous, installed_version, allow_additive=allow_additive,
            )

        asyncio.run(smoke._exercise(
            root, "profiles_restart" if previous else "profiles", result,
            record_history=True, after_scenario=probe, **exercise_options,
        ))
        result["success"] = True
    except Exception as error:
        logging.getLogger(__name__).exception("Core upgrade phase failed")
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        result["network_guard"] = {"blocked": guard.blocked, "allowed_loopback_connects": guard.allowed_connects}
        output.write_text(json.dumps(result, indent=2, default=smoke._json_default) + "\n", encoding="utf-8")
    return 0 if result["success"] else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "phase":
        phase_parser = argparse.ArgumentParser(description=__doc__)
        phase_parser.add_argument("phase")
        phase_parser.add_argument("root", type=Path)
        phase_parser.add_argument("output", type=Path)
        phase_parser.add_argument("previous", type=Path, nargs="?")
        phase_parser.add_argument("--allow-additive", action="store_true")
        phase_args = phase_parser.parse_args()
        return _phase(
            phase_args.root,
            phase_args.output,
            phase_args.previous,
            allow_additive=phase_args.allow_additive,
        )
    if len(sys.argv) > 1 and sys.argv[1] == "pr-applicable":
        app_parser = argparse.ArgumentParser(description=__doc__)
        app_parser.add_argument("pr_applicable")
        app_parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
        app_parser.add_argument("--output", type=Path, required=True)
        app_parser.add_argument("--github-output", type=Path)
        app_args = app_parser.parse_args()
        decision = select_pr_upgrade_applicability(app_args.repo, dict(os.environ))
        app_args.output.parent.mkdir(parents=True, exist_ok=True)
        app_args.output.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if app_args.github_output is not None:
            append_github_output(
                app_args.github_output,
                {"run_upgrade": "true" if decision["run_upgrade"] else "false"},
            )
        print(json.dumps(decision, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-bundle", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--release-zip", type=Path, required=True)
    parser.add_argument("--allow-additive", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"schema": 1, "scenario": "upgrade", "success": False, "temporary_config_removed": False}
    try:
        with args.output.open("x", encoding="utf-8", newline="\n") as receipt:
            try:
                smoke._require(sys.platform == "linux", "Upgrade acceptance requires the Linux core-current Docker image")
                old = load_artifact(args.from_bundle)
                new = load_artifact(args.bundle)
                result.update(from_artifact=old, to_artifact=new)
                with args.release_zip.open("rb") as package_file:
                    result["release_zip_sha256"] = hashlib.file_digest(package_file, "sha256").hexdigest()
                with tempfile.TemporaryDirectory(prefix="uplus-upgrade-") as temporary:
                    directory = Path(temporary)
                    root = directory / "config"
                    extract_artifact(args.from_bundle, root)
                    for phase in ("before", "after"):
                        if phase == "after":
                            incoming = directory / "incoming"
                            expected = {
                                name.removeprefix(f"{smoke.PACKAGE.as_posix()}/")
                                for name in new["files"]
                            }
                            with ZipFile(args.release_zip) as package:
                                names = package.namelist()
                                smoke._require(
                                    len(names) == len(expected) and set(names) == expected,
                                    "Release ZIP differs from the validated artifact member set",
                                )
                                package.extractall(incoming / smoke.PACKAGE)
                            verify_installed(new, incoming)
                            (root / smoke.PACKAGE).rename(directory / "old-component")
                            (incoming / smoke.PACKAGE).rename(root / smoke.PACKAGE)
                        phase_output = directory / f"{phase}.json"
                        command = [sys.executable, str(Path(__file__).resolve()), "phase", str(root), str(phase_output)]
                        if phase == "after":
                            command.append(str(directory / "before.json"))
                            if args.allow_additive:
                                command.append("--allow-additive")
                        completed = subprocess.run(command, capture_output=True, text=True, timeout=330, check=False)
                        result[f"{phase}_core_log"] = completed.stderr
                        if phase_output.exists():
                            result[phase] = json.loads(phase_output.read_text(encoding="utf-8"))
                        smoke._require(completed.returncode == 0, f"{phase} Core failed: {result.get(phase, {}).get('error', completed.stderr)}")
                        verify_installed(new if phase == "after" else old, root)
                    result["success"] = True
            except (OSError, ValueError, RuntimeError, BadZipFile, subprocess.TimeoutExpired) as error:
                result["error"] = f"{type(error).__name__}: {error}"
            finally:
                result["temporary_config_removed"] = "temporary" in locals() and not Path(temporary).exists()
                json.dump(result, receipt, indent=2, default=smoke._json_default)
                receipt.write("\n")
    except OSError as error:
        print(f"upgrade_smoke: {error}", file=sys.stderr)
        return 1
    if not result["success"]:
        print(f"upgrade_smoke: {result.get('error', 'failed')}", file=sys.stderr)
        return 1
    print(f"upgrade_smoke: profiles, identity, preferences, units, history and statistics preserved; evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
