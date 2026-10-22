"""Offline release-asset rejection boundaries; actual HACS is a separate scenario."""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hacs_acceptance import main, verify_release_package
from hacs_fixture import InstallerGuard, extract_hacs, safe_error


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.commit = "a" * 40
        self.files = {
            "manifest.json": b'{"domain":"uplus_reborn","version":"1.0.1"}',
            "LICENSE": b"MIT license fixture",
            "__init__.py": b"original = True\n",
        }
        self.tree = {"truncated": False, "tree": [
            {"path": "custom_components/uplus_reborn/" + name, "type": "blob", "mode": "100644",
             "sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()}
            for name, data in self.files.items()
        ]}
        self.release = {"tag_name": "v1.0.1", "draft": False, "prerelease": False, "immutable": True}

    def package(self, files):
        with zipfile.ZipFile(self.root / "uplus_reborn.zip", "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        digest = hashlib.sha256((self.root / "uplus_reborn.zip").read_bytes()).hexdigest()
        (self.root / "SHA256SUMS").write_text(f"{digest}  uplus_reborn.zip\n", encoding="utf-8")
        (self.root / "release.json").write_text(json.dumps({
            "schema": 1, "commit": self.commit, "version": "1.0.1", "asset": "uplus_reborn.zip",
            "sha256": digest, "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }), encoding="utf-8")

    def test_replaced_assets_cannot_forge_the_immutable_tag_source(self):
        self.package({**self.files, "__init__.py": b"original = False\n"})
        with self.assertRaisesRegex(ValueError, "committed"):
            verify_release_package(self.root, self.release, self.commit, self.tree)

    def test_extra_traversal_member_is_rejected(self):
        self.package({**self.files, "../outside.py": b"escape"})
        with self.assertRaises(ValueError):
            verify_release_package(self.root, self.release, self.commit, self.tree)

    def test_a_tagged_source_with_the_wrong_manifest_version_is_rejected(self):
        self.files["manifest.json"] = b'{"domain":"uplus_reborn","version":"1.0.2"}'
        data = self.files["manifest.json"]
        self.tree["tree"][0]["sha"] = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False,
        ).hexdigest()
        self.package(self.files)
        with self.assertRaisesRegex(ValueError, "version"):
            verify_release_package(self.root, self.release, self.commit, self.tree)

    def test_checksum_mismatch_and_nonimmutable_release_are_rejected(self):
        self.package(self.files)
        (self.root / "SHA256SUMS").write_text("wrong\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_release_package(self.root, self.release, self.commit, self.tree)
        with self.assertRaisesRegex(ValueError, "immutable"):
            verify_release_package(self.root, {**self.release, "immutable": False}, self.commit, self.tree)


class InstallerIsolationTests(unittest.TestCase):
    def test_only_approved_https_and_loopback_are_allowed(self):
        guard = InstallerGuard({"192.0.2.1"})
        with socket.socket() as connection:
            guard.audit("socket.connect", (connection, ("192.0.2.1", 443)))
            for destination in (("192.0.2.1", 80), ("192.0.2.2", 443), ("10.0.0.1", 56800)):
                with self.subTest(destination=destination), self.assertRaises(PermissionError):
                    guard.audit("socket.connect", (connection, destination))
            guard.github_enabled = False
            with self.assertRaises(PermissionError):
                guard.audit("socket.connect", (connection, ("192.0.2.1", 443)))
            guard.audit("socket.connect", (connection, ("127.0.0.1", 56800)))

    def test_signed_download_queries_are_not_recorded_in_errors(self):
        message = safe_error(ValueError("https://release-assets.githubusercontent.com/file?sig=private"))
        self.assertNotIn("sig=private", message)
        self.assertIn("[redacted]", message)


class LauncherFailureTests(unittest.TestCase):
    def test_unavailable_docker_records_a_blocked_result_before_any_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "attempt"
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("hacs_acceptance.py")),
                 "--tag", "v1.0.1", "--baseline", "v1.0.0", "--output", str(output)],
                env={**os.environ, "PATH": ""}, capture_output=True, text=True,
                timeout=30, check=False,
            )
            receipt = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertFalse(receipt["success"])
            self.assertIn("DOCKER_UNAVAILABLE", receipt["error"])
            self.assertFalse((output / "inputs").exists())

    def test_concurrent_output_owner_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "attempt"
            original_mkdir = Path.mkdir

            def concurrent_mkdir(path, *args, **kwargs):
                if path == output:
                    original_mkdir(path, *args, **kwargs)
                    (path / "acceptance.json").write_text("other attempt", encoding="utf-8")
                return original_mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", concurrent_mkdir), redirect_stdout(io.StringIO()):
                code = main(["--tag", "v1.0.1", "--baseline", "v1.0.0", "--output", str(output)])
            self.assertNotEqual(code, 0)
            self.assertEqual((output / "acceptance.json").read_text(encoding="utf-8"), "other attempt")


class UpstreamArchiveTests(unittest.TestCase):
    def test_upstream_bytecode_is_allowed_but_traversal_and_wrong_hash_never_extract(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "hacs.zip"
            member = "hacs_frontend/__pycache__/bundle.cpython-313.pyc"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(member, b"trusted upstream package bytecode")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            extract_hacs(directory / "valid", archive, {"hacs_sha256": digest})
            installed = directory / "valid/custom_components/hacs"
            self.assertEqual({p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file()}, {member})
            with self.assertRaisesRegex(RuntimeError, "bytes changed"):
                extract_hacs(directory / "wrong-hash", archive, {"hacs_sha256": "0" * 64})
            self.assertFalse((directory / "wrong-hash").exists())
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside", b"escape")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                extract_hacs(directory / "unsafe", archive, {
                    "hacs_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                })
            self.assertFalse((directory / "unsafe").exists())
            self.assertFalse((directory / "outside").exists())


if __name__ == "__main__":
    unittest.main()
