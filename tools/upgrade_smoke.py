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
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import ha_smoke as smoke
from delivery import extract_artifact, load_artifact, verify_installed


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


async def _checkpoint(hass, entry, peer, result: dict, previous: dict | None) -> None:
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
        smoke._require(evidence["registry"] == old["registry"], "Upgrade changed identity, preferences or sensor units/classes")
        smoke._require(all(row in recorded for row in old["history"]), "Upgrade lost or rescaled historical states")
        smoke._require(hourly == old["statistics"], "Upgrade changed historical consumption statistics")
        smoke._require(evidence["statistic_identity"] == old["statistic_identity"], "Upgrade changed statistic identity or units")
        result["upgrade_preservation_verified"] = True
    result["upgrade"] = evidence


def _phase(root: Path, output: Path, previous_path: Path | None) -> int:
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
        async def probe(hass, entry, peer):
            await _checkpoint(hass, entry, peer, result, previous)

        asyncio.run(smoke._exercise(
            root, "profiles_restart" if previous else "profiles", result,
            record_history=True, after_scenario=probe,
        ))
        result["integration_version"] = json.loads((root / smoke.PACKAGE / "manifest.json").read_text())["version"]
        result["success"] = True
    except Exception as error:
        logging.getLogger(__name__).exception("Core upgrade phase failed")
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        result["network_guard"] = {"blocked": guard.blocked, "allowed_loopback_connects": guard.allowed_connects}
        output.write_text(json.dumps(result, indent=2, default=smoke._json_default) + "\n", encoding="utf-8")
    return 0 if result["success"] else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_phase":
        return _phase(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]) if len(sys.argv) > 4 else None)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-bundle", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--release-zip", type=Path, required=True)
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
                        command = [sys.executable, str(Path(__file__).resolve()), "_phase", str(root), str(phase_output)]
                        if phase == "after":
                            command.append(str(directory / "before.json"))
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
