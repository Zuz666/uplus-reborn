"""Container worker for hacs_acceptance.py; real HACS, no live credentials."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urljoin, urlparse
from zipfile import ZipFile

import ha_smoke as smoke
from delivery import archive_member_name, verify_installed

HOSTS = frozenset({"api.github.com", "github.com", "codeload.github.com",
                   "raw.githubusercontent.com", "release-assets.githubusercontent.com"})
CALVER_CORE_RE = re.compile(r"(\d{4})\.([1-9]\d*|0)\.(?:0|[1-9]\d*)(?:[a-zA-Z0-9._-]*)?\Z")


def installer_mode_for_core(core_version: str) -> str:
    """Select built-in Marketplace on HA 2026.11+ or legacy HACS on earlier Core."""
    match = CALVER_CORE_RE.fullmatch(core_version)
    if match is None:
        raise ValueError(f"Invalid Home Assistant Core version: {core_version!r}")
    year, month = int(match.group(1)), int(match.group(2))
    return "marketplace" if (year, month) >= (2026, 11) else "hacs"


def sanitize_text(text: str, *, limit: int = 4000, tail: bool = False) -> str:
    """Redact signed URLs and GitHub tokens while bounding log size."""
    value = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|token)\s+)\S+", r"\1[redacted]", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[redacted]", value)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(key)
        if token:
            value = value.replace(token, "[redacted]")
    if len(value) <= limit:
        return value
    if tail:
        prefix = "...[truncated]\n"
        return prefix + value[-(limit - len(prefix)):] if limit > len(prefix) else value[-limit:]
    return value[:limit]


def safe_error(error: Exception) -> str:
    return sanitize_text(f"{type(error).__name__}: {error}", limit=1000, tail=False)


class InstallerGuard(smoke.LoopbackGuard):
    def __init__(self, addresses):
        super().__init__()
        self.addresses = addresses
        self.github_enabled = True
        self.github_connections = 0

    def audit(self, event, args):
        if self.github_enabled:
            if event == "socket.getaddrinfo" and args[0] in HOSTS:
                return
            if event == "socket.connect":
                sock, address = args
                if (sock.type & 0xF == socket.SOCK_STREAM and isinstance(address, tuple)
                        and address[0] in self.addresses and address[1] == 443):
                    self.github_connections += 1
                    return
        super().audit(event, args)


async def install(args, expected, addresses, guard, result):
    import aiohttp
    from aiohttp.abc import AbstractResolver
    from awesomeversion import AwesomeVersion
    from homeassistant import bootstrap, loader
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.const import __version__ as core_version
    from homeassistant.core import HomeAssistant

    class PinnedResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            smoke._require(host in addresses, "Unapproved DNS host")
            return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                     "proto": 0, "flags": 0} for ip in addresses[host]]

        async def close(self):
            return None

    result.update(core_version=core_version, http_requests=[])
    trace = aiohttp.TraceConfig()

    def check_url(url, headers):
        parsed = urlparse(str(url))
        smoke._require(parsed.scheme == "https" and parsed.hostname in HOSTS
                       and parsed.port in (None, 443) and not parsed.username, "Forbidden installer URL")
        smoke._require(not headers.get("Authorization") or parsed.hostname == "api.github.com",
                       "Authorization cannot leave the GitHub API host")
        result["http_requests"].append(f"https://{parsed.hostname}{parsed.path}")

    async def on_request(session, context, params):
        check_url(params.url, params.headers)

    async def on_redirect(session, context, params):
        check_url(urljoin(str(params.url), params.response.headers["Location"]), params.headers)

    trace.on_request_start.append(on_request)
    trace.on_request_redirect.append(on_redirect)
    hass = HomeAssistant(str(args.root))
    loader.async_setup(hass)
    hass.config.skip_pip = True
    hass.config_entries = ConfigEntries(hass, {})
    try:
        await bootstrap.async_load_base_functionality(hass)
        await loader.async_get_custom_components(hass)
        connector = aiohttp.TCPConnector(resolver=PinnedResolver(), family=socket.AF_INET)
        fixture = json.loads(Path("/inputs/fixture.json").read_text())
        installer_mode = fixture.get("installer", "hacs")
        smoke._require(installer_mode in {"hacs", "marketplace"}, f"Unsupported installer mode: {installer_mode}")
        async with aiohttp.ClientSession(connector=connector, trace_configs=[trace]) as session:
            if installer_mode == "marketplace":
                smoke._require(
                    fixture.get("expected_core_version") is None or core_version == fixture["expected_core_version"],
                    "Unexpected Core version for Marketplace fixture",
                )
                repository, version_target, min_attr, install_fn = await _prepare_marketplace(
                    hass, loader, session, args, result,
                )
            else:
                repository, version_target, min_attr, install_fn = await _prepare_hacs(
                    hass, loader, session, args, fixture, core_version, result,
                )
            smoke._require(
                repository.repository_manifest.zip_release
                and repository.repository_manifest.filename == "uplus_reborn.zip"
                and repository.data.domain == smoke.DOMAIN,
                "Wrong HACS/Marketplace installation contract",
            )
            result["available_version"] = repository.display_available_version
            if args.expect_latest:
                smoke._require(
                    repository.display_available_version == args.latest,
                    "Installer latest selection differs",
                )
            native_version = version_target.version
            minimum = repository.repository_manifest.homeassistant
            smoke._require(minimum is not None, "Missing minimum Home Assistant Core version")
            try:
                version_target.version = AwesomeVersion("0.0.0")
                smoke._require(not getattr(repository, min_attr), "Installer ignored the minimum Core")
                version_target.version = AwesomeVersion(minimum)
                smoke._require(getattr(repository, min_attr), "Installer rejected the declared minimum")
            finally:
                version_target.version = native_version
            smoke._require(getattr(repository, min_attr), "Current fixture Core is below the supported minimum")
            result["minimum_selection_verified"] = minimum
            if args.previous:
                previous = json.loads(args.previous.read_text())
                repository.data.update_data(previous["repository_data"])
                smoke._require(
                    repository.data.installed and repository.data.installed_version == args.baseline,
                    "Missing actual baseline installer metadata",
                )
                smoke._require(repository.pending_update, "Installer did not detect the update")
                result["pending_update_before"] = repository.pending_update
            await install_fn()
            smoke._require(
                repository.data.installed and repository.data.installed_version == args.tag,
                "Installer installed the wrong version",
            )
            verify_installed(expected, args.root)
            manifest = json.loads((args.root / smoke.PACKAGE / "manifest.json").read_text())
            smoke._require(manifest["version"] == args.tag[1:], "Installed manifest version differs")
            result.update(installed_version=repository.data.installed_version,
                          installed_hashes=smoke._installed_hashes(args.root),
                          repository_data=repository.data.to_json(), source_commit=expected["commit"],
                          pending_update_after=repository.pending_update)
    finally:
        guard.github_enabled = False
        await hass.async_stop(force=True)


async def _init_marketplace_manager(hass, loader, session, result):
    """Initialize HA 2026.11+ built-in Marketplace with an explicit HTTP session."""
    from homeassistant.components.lovelace import LOVELACE_DATA
    from homeassistant.components.marketplace import base as marketplace_base
    from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE_DOMAIN
    from homeassistant.components.marketplace.enums import (
        LovelaceMode,
        RepositoryCategory,
    )
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import CONF_TOKEN

    integration = await loader.async_get_integration(hass, MARKETPLACE_DOMAIN)
    smoke._require(integration.is_built_in, "Expected built-in Home Assistant Marketplace")
    hass.data[LOVELACE_DATA] = SimpleNamespace(resource_mode=LovelaceMode.STORAGE)
    token = os.environ.get("GH_TOKEN") or None
    entry = ConfigEntry(
        version=1, minor_version=1, domain=MARKETPLACE_DOMAIN,
        title="Marketplace", source="system", unique_id=MARKETPLACE_DOMAIN,
        data={CONF_TOKEN: token} if token else {}, options={},
        discovery_keys=MappingProxyType({}), subentries_data=(),
    )
    original_clientsession = marketplace_base.async_get_clientsession
    marketplace_base.async_get_clientsession = lambda _hass, *_args, **_kwargs: session
    try:
        marketplace = marketplace_base.MarketplaceManager(hass, entry)
    finally:
        marketplace_base.async_get_clientsession = original_clientsession
    smoke._require(marketplace.session is session, "Marketplace bypassed pinned HTTP session")
    marketplace.status.startup = False
    marketplace.enable_category(RepositoryCategory.INTEGRATION)
    result.update(installer="marketplace", marketplace_version=str(marketplace.version))
    return marketplace, RepositoryCategory


async def _prepare_marketplace(hass, loader, session, args, result):
    """Register and install target repository through HA 2026.11+ built-in Marketplace."""
    marketplace, RepositoryCategory = await _init_marketplace_manager(hass, loader, session, result)
    errors = await marketplace.async_register_repository(args.repository, RepositoryCategory.INTEGRATION)
    repository = marketplace.repositories.get_by_full_name(args.repository)
    smoke._require(repository is not None and not errors and not repository.validate.errors,
                   "Marketplace registration failed")
    target_manifest = await repository.get_repository_manifest(version=args.tag)
    smoke._require(target_manifest is not None, "Missing hacs.json for target tag")
    repository.repository_manifest = target_manifest

    class _VersionTarget:
        @property
        def version(self):
            return marketplace.version

        @version.setter
        def version(self, value) -> None:
            marketplace.version = value

    async def install_tag() -> None:
        await repository.async_install_repository(ref=args.tag)

    return repository, _VersionTarget(), "can_install", install_tag


async def _prepare_hacs(hass, loader, session, args, fixture, core_version, result):
    """Initialize standalone custom HACS (< 2026.11 Core) for bounded registration."""
    from aiogithubapi import GitHub, GitHubAPI
    from aiogithubapi.const import ACCEPT_HEADERS
    from awesomeversion import AwesomeVersion
    from custom_components.hacs.base import HacsBase
    from custom_components.hacs.enums import HacsCategory

    hacs = HacsBase()
    hacs.hass = hass
    hacs.integration = await loader.async_get_integration(hass, "hacs")
    hacs.version = hacs.integration.version
    smoke._require(str(hacs.version) == fixture["hacs_version"], "Wrong official HACS version")
    result.update(installer="hacs", hacs_version=str(hacs.version))
    hacs.core.config_path = str(args.root)
    hacs.core.ha_version = AwesomeVersion(core_version)
    hacs.system.running = True
    hacs.status.startup = False
    hacs.session = session
    token = os.environ.get("GH_TOKEN") or None
    hacs.github = GitHub(token, session, headers={"User-Agent": "HACS/" + fixture["hacs_version"],
                                                 "Accept": ACCEPT_HEADERS["preview"]})
    hacs.githubapi = GitHubAPI(token=token, session=session, client_name="HACS/" + fixture["hacs_version"])
    await hacs.async_register_repository(args.repository, HacsCategory.INTEGRATION, ref=args.tag)
    repository = hacs.repositories.get_by_full_name(args.repository)
    smoke._require(repository is not None and not repository.validate.errors, "HACS registration failed")

    class _VersionTarget:
        @property
        def version(self):
            return hacs.core.ha_version

        @version.setter
        def version(self, value) -> None:
            hacs.core.ha_version = value

    async def install_tag() -> None:
        await repository.async_install(version=args.tag)

    return repository, _VersionTarget(), "can_download", install_tag


def installer_main(args):
    result = {"success": False}
    started = time.monotonic()
    try:
        expected = json.loads(args.expected.read_text())
        addresses = {host: sorted({row[4][0] for row in socket.getaddrinfo(
            host, 443, socket.AF_INET, socket.SOCK_STREAM)}) for host in HOSTS}
        guard = InstallerGuard({ip for values in addresses.values() for ip in values})
        sys.addaudithook(guard.audit)
        sys.path.insert(0, str(args.root))
        asyncio.run(install(args, expected, addresses, guard, result))
        result["success"] = True
    except Exception as error:
        result["error"] = safe_error(error)
        logging.getLogger(__name__).exception("HACS installer phase failed")
    finally:
        result["duration_seconds"] = time.monotonic() - started
        if "guard" in locals():
            result["network_guard"] = {"github_connections": guard.github_connections, "blocked": guard.blocked}
        with args.receipt.open("x", encoding="utf-8") as receipt:
            json.dump(result, receipt, indent=2, default=smoke._json_default)
    return 0 if result["success"] else 1


def extract_hacs(root, archive_path, fixture):
    smoke._require(hashlib.sha256(archive_path.read_bytes()).hexdigest() == fixture["hacs_sha256"],
                   "Official HACS bytes changed")
    with ZipFile(archive_path) as archive:
        smoke._require(sum(row.file_size for row in archive.infolist()) <= 128 * 1024 * 1024,
                       "Official HACS archive is too large")
        for row in archive.infolist():
            archive_member_name(row.filename, directory=row.is_dir())
            smoke._require(not stat.S_ISLNK(row.external_attr >> 16), "Unsafe HACS archive link")
        archive.extractall(root / "custom_components" / "hacs")


def run_phase(name, command, output, result, *, installer=False):
    """Run a fresh worker; only the installer receives GitHub read credentials."""
    phase_started = time.monotonic()
    environment = dict(os.environ)
    environment.pop("GITHUB_TOKEN", None)
    if not installer:
        environment.pop("GH_TOKEN", None)
    completed = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=330, check=False,
    )
    path = output / f"{name}.json"
    phase = json.loads(path.read_text()) if path.exists() else {}
    ok = completed.returncode == 0 and bool(phase.get("success"))
    phase_entry = {
        "returncode": completed.returncode,
        "duration_seconds": time.monotonic() - phase_started,
        "receipt": phase,
    }
    if not ok:
        stderr_tail = sanitize_text((completed.stderr or "").strip(), limit=4000, tail=True)
        if stderr_tail:
            phase_entry["stderr"] = stderr_tail
    result["phases"][name] = phase_entry
    smoke._require(ok, f"{name} failed: {phase.get('error', 'worker failed without a receipt')}")


async def _run_self_check(root: Path, result: dict) -> None:
    """Verify Core Marketplace/HACS installer APIs offline inside the container."""
    import aiohttp
    from homeassistant import bootstrap, loader
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.const import __version__ as core_version
    from homeassistant.core import HomeAssistant

    mode = installer_mode_for_core(core_version)
    result.update(core_version=core_version, installer=mode)
    hass = HomeAssistant(str(root))
    loader.async_setup(hass)
    hass.config.skip_pip = True
    hass.config_entries = ConfigEntries(hass, {})
    try:
        await bootstrap.async_load_base_functionality(hass)
        await loader.async_get_custom_components(hass)
        if mode == "marketplace":
            from homeassistant.components.marketplace.repositories.integration import (
                IntegrationRepository,
            )

            async with aiohttp.ClientSession() as session:
                marketplace, _ = await _init_marketplace_manager(hass, loader, session, result)
            manager_attrs = ("async_register_repository", "enable_category", "repositories", "version")
            for attr in manager_attrs:
                smoke._require(hasattr(marketplace, attr), f"MarketplaceManager missing {attr}")
            smoke._require(
                callable(getattr(marketplace.repositories, "get_by_full_name", None)),
                "MarketplaceManager.repositories missing get_by_full_name",
            )
            repo_attrs = (
                "get_repository_manifest",
                "can_install",
                "async_install_repository",
                "display_available_version",
                "pending_update",
            )
            for attr in repo_attrs:
                smoke._require(hasattr(IntegrationRepository, attr), f"IntegrationRepository missing {attr}")
            result["verified_symbols"] = [*manager_attrs, "repositories.get_by_full_name", *repo_attrs]
        else:
            blocked = getattr(loader, "BLOCKED_CUSTOM_INTEGRATIONS", {})
            smoke._require("hacs" not in blocked, "Core blocked custom integration 'hacs'")
            result["verified_symbols"] = ["hacs_not_blocked"]
    finally:
        await hass.async_stop(force=True)


def self_check_main(args) -> int:
    """Run an offline, credential-free Core installer compatibility self-check."""
    result = {"schema": 1, "scenario": "installer-self-check", "success": False, "temporary_config_removed": False}
    started = time.monotonic()
    guard = smoke.LoopbackGuard()
    sys.addaudithook(guard.audit)
    try:
        with tempfile.TemporaryDirectory(prefix="uplus-installer-check-") as temporary:
            directory = Path(temporary)
            asyncio.run(_run_self_check(directory, result))
        result["success"] = True
    except Exception as error:
        result["error"] = safe_error(error)
        logging.getLogger(__name__).exception("Installer self-check failed")
    finally:
        result["duration_seconds"] = time.monotonic() - started
        result["temporary_config_removed"] = "directory" in locals() and not directory.exists()
        result["network_guard"] = {"blocked": guard.blocked, "allowed_loopback_connects": guard.allowed_connects}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as receipt:
            json.dump(result, receipt, indent=2, default=smoke._json_default)
            receipt.write("\n")
    print(json.dumps({
        key: result[key]
        for key in ("success", "core_version", "installer", "temporary_config_removed", "error")
        if key in result
    }))
    return 0 if result["success"] else 1


def main(args):
    result = {"schema": 1, "success": False, "phases": {}, "temporary_configs_removed": False}
    output = Path("/evidence")
    started = time.monotonic()

    def install_phase(name, root, target, previous=None):
        tag = args.baseline if target == "baseline" else args.tag
        command = [sys.executable, str(Path(__file__).resolve()), "installer", "--root", str(root),
                   "--expected", f"/inputs/{target}/expected.json", "--repository", args.repository,
                   "--tag", tag, "--latest", args.tag, "--receipt", str(output / f"{name}.json")]
        if args.expect_latest and target == "target":
            command.append("--expect-latest")
        if previous:
            command.extend(["--previous", str(output / f"{previous}.json"), "--baseline", args.baseline])
        run_phase(name, command, output, result, installer=True)

    def core_phase(name, root, previous=None):
        command = [sys.executable, "/workspace/tools/upgrade_smoke.py", "phase", str(root),
                   str(output / f"{name}.json")]
        if previous:
            command.append(str(output / f"{previous}.json"))
        run_phase(name, command, output, result)

    def prepare_root(path: Path, mode: str, fixture: dict) -> None:
        if mode == "hacs":
            extract_hacs(path, Path("/inputs/hacs.zip"), fixture)
        else:
            path.mkdir(parents=True, exist_ok=True)

    try:
        fixture = json.loads(Path("/inputs/fixture.json").read_text())
        installer_mode = fixture.get("installer", "hacs")
        smoke._require(installer_mode in {"hacs", "marketplace"}, f"Unsupported installer mode: {installer_mode}")
        result["installer"] = installer_mode
        with tempfile.TemporaryDirectory(prefix="uplus-hacs-") as temporary:
            directory = Path(temporary)
            fresh = directory / "fresh"
            prepare_root(fresh, installer_mode, fixture)
            install_phase("fresh_install", fresh, "target")
            core_phase("fresh_boot", fresh)
            core_phase("fresh_restart", fresh, "fresh_boot")
            if args.baseline:
                upgrade = directory / "upgrade"
                prepare_root(upgrade, installer_mode, fixture)
                install_phase("baseline_install", upgrade, "baseline")
                core_phase("upgrade_before", upgrade)
                install_phase("upgrade_install", upgrade, "target", "baseline_install")
                core_phase("upgrade_after", upgrade, "upgrade_before")
        result["success"] = True
    except Exception as error:
        result["error"] = safe_error(error)
        logging.getLogger(__name__).exception("HACS acceptance failed")
    finally:
        result["duration_seconds"] = time.monotonic() - started
        result["temporary_configs_removed"] = "directory" in locals() and not directory.exists()
        Path("/evidence/result.json").write_text(json.dumps(result, indent=2, default=smoke._json_default) + "\n")
    print(json.dumps({key: result[key] for key in ("success", "error", "temporary_configs_removed") if key in result}))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    if len(sys.argv) > 1 and sys.argv[1] == "self-check":
        check_parser = argparse.ArgumentParser(description=__doc__)
        check_parser.add_argument("self_check")
        check_parser.add_argument("--output", type=Path, required=True)
        raise SystemExit(self_check_main(check_parser.parse_args()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--expect-latest", action="store_true")
    if len(sys.argv) > 1 and sys.argv[1] == "installer":
        parser.add_argument("installer")
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--expected", type=Path, required=True)
        parser.add_argument("--receipt", type=Path, required=True)
        parser.add_argument("--previous", type=Path)
        parser.add_argument("--latest", required=True)
        raise SystemExit(installer_main(parser.parse_args()))
    raise SystemExit(main(parser.parse_args()))
