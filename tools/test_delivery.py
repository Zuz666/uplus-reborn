"""Standalone delivery contracts: real Git, tar files, subprocesses and receipts.

Run with: python -m unittest discover -s tools -p test_delivery.py -v
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import delivery

TOOL = Path(__file__).with_name("delivery.py").resolve()
PACKAGE = delivery.PACKAGE


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def cli(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, str(TOOL), *map(str, arguments)],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"Host does not permit symlinks: {error}")


class ArtifactTests(Workspace):
    def git(self, *arguments):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode().strip()

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "true")
        self.git("config", "core.eol", "crlf")
        package = self.repo / PACKAGE
        package.mkdir(parents=True)
        (self.repo / ".gitattributes").write_bytes(b"*.py text eol=lf\n*.json text eol=lf\n")
        (package / "manifest.json").write_bytes(b'{"domain":"uplus_reborn"}\n')
        (package / "__init__.py").write_bytes(b'VALUE = "committed"\n')
        self.commit = self.commit_files()
        self.bundle = self.root / "bundle"

    def commit_files(self):
        self.git("add", "--all")
        self.git("-c", "user.name=Delivery Test", "-c", "user.email=test@example.invalid",
                 "commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def build(self):
        self.cli("build", "--repo", self.repo, "--commit", self.commit, "--output", self.bundle)
        return delivery.load_artifact(self.bundle)

    def test_commit_only_lf_build_extract_and_full_installed_comparison(self):
        (self.repo / PACKAGE / "__init__.py").write_bytes(b'VALUE = "dirty"\r\n')
        (self.repo / PACKAGE / "untracked.py").write_bytes(b"untracked\n")
        manifest = self.build()
        installed = self.root / "installed"
        self.assertEqual(delivery.extract_artifact(self.bundle, installed), manifest)
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual((installed / PACKAGE / "__init__.py").read_bytes(), b'VALUE = "committed"\n')
        self.assertFalse((installed / PACKAGE / "untracked.py").exists())
        cache = installed / PACKAGE / "__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-313.pyc").write_bytes(b"generated")
        self.cli("verify", "--bundle", self.bundle, "--installed", installed)
        extra = installed / PACKAGE / "unexpected.py"
        extra.write_bytes(b"extra")
        self.cli("verify", "--bundle", self.bundle, "--installed", installed, expected=1)
        extra.unlink()
        (installed / PACKAGE / "__init__.py").write_bytes(b"changed")
        self.cli("verify", "--bundle", self.bundle, "--installed", installed, expected=1)
        (installed / PACKAGE / "__init__.py").unlink()
        self.cli("verify", "--bundle", self.bundle, "--installed", installed, expected=1)

    def test_existing_bundle_and_destination_are_not_overwritten(self):
        self.build()
        original = (self.bundle / "artifact.tar.gz").read_bytes()
        self.cli("build", "--repo", self.repo, "--commit", self.commit,
                 "--output", self.bundle, expected=1)
        self.assertEqual((self.bundle / "artifact.tar.gz").read_bytes(), original)
        destination = self.root / "occupied"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            delivery.extract_artifact(self.bundle, destination)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_selected_commit_is_not_replaced_by_newer_head(self):
        (self.repo / PACKAGE / "__init__.py").write_bytes(b'VALUE = "new head"\n')
        newer = self.commit_files()
        self.assertNotEqual(newer, self.commit)
        manifest = self.build()
        installed = self.root / "installed"
        delivery.extract_artifact(self.bundle, installed)
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual((installed / PACKAGE / "__init__.py").read_bytes(), b'VALUE = "committed"\n')

    def test_export_ignore_cannot_silently_omit_committed_members(self):
        attributes = self.repo / ".gitattributes"
        attributes.write_bytes(attributes.read_bytes() + b"custom_components/uplus_reborn/__init__.py export-ignore\n")
        self.commit = self.commit_files()
        self.cli("build", "--repo", self.repo, "--commit", self.commit,
                 "--output", self.bundle, expected=1)
        self.assertFalse(self.bundle.exists())

    def test_export_substitution_cannot_change_committed_bytes(self):
        attributes = self.repo / ".gitattributes"
        attributes.write_bytes(attributes.read_bytes() + b"custom_components/uplus_reborn/__init__.py export-subst\n")
        (self.repo / PACKAGE / "__init__.py").write_bytes(b'# $Format:%H$\n')
        self.commit = self.commit_files()
        self.cli("build", "--repo", self.repo, "--commit", self.commit,
                 "--output", self.bundle, expected=1)
        self.assertFalse(self.bundle.exists())

    def test_committed_link_is_rejected_without_following_target(self):
        # Index mode tests this contract even on Windows hosts without link privileges.
        blob = self.git("rev-parse", f"{self.commit}:{PACKAGE}/manifest.json")
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{PACKAGE}/linked")
        self.git("-c", "user.name=Delivery Test", "-c", "user.email=test@example.invalid",
                 "commit", "--quiet", "-m", "link fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.cli("build", "--repo", self.repo, "--commit", self.commit,
                 "--output", self.bundle, expected=1)
        self.assertFalse(self.bundle.exists())

    def test_missing_revision_fails_without_creating_bundle(self):
        self.cli("build", "--repo", self.repo, "--commit", "no-such-commit",
                 "--output", self.bundle, expected=1)
        self.assertFalse(self.bundle.exists())

    def test_corrupted_archive_fails_before_destination_creation(self):
        self.build()
        with (self.bundle / "artifact.tar.gz").open("ab") as stream:
            stream.write(b"corruption")
        destination = self.root / "not-created"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            delivery.extract_artifact(self.bundle, destination)
        self.assertFalse(destination.exists())

    def test_installed_cache_link_is_not_ignored(self):
        manifest = self.build()
        installed = self.root / "installed"
        delivery.extract_artifact(self.bundle, installed)
        cache = installed / PACKAGE / "__pycache__"
        cache.mkdir()
        self.symlink(cache / "hidden.pyc", self.repo / PACKAGE / "manifest.json")
        with self.assertRaisesRegex(ValueError, "Links"):
            delivery.verify_installed(manifest, installed)

    def test_linked_extraction_ancestor_is_rejected(self):
        self.build()
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        self.symlink(linked, real, directory=True)
        with self.assertRaisesRegex(ValueError, "Links"):
            delivery.extract_artifact(self.bundle, linked / "child")
        self.assertFalse((real / "child").exists())


class UntrustedArchiveTests(Workspace):
    def fixture(self, members, *, manifest_files=None):
        bundle = self.root / "bundle"
        bundle.mkdir(exist_ok=True)
        files = {}
        with tarfile.open(bundle / "artifact.tar.gz", "w:gz") as archive:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(data)
                    files[name] = hashlib.sha256(data).hexdigest()
                    archive.addfile(info, io.BytesIO(data))
                else:
                    info.linkname = "../../outside" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
                    archive.addfile(info)
        manifest = {
            "schema": 1, "commit": "a" * 40,
            "archive_sha256": hashlib.sha256((bundle / "artifact.tar.gz").read_bytes()).hexdigest(),
            "files": files if manifest_files is None else manifest_files,
        }
        (bundle / "artifact.json").write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    def test_unsafe_archive_members_never_extract(self):
        valid = (PACKAGE + "/safe.py", tarfile.REGTYPE, b"safe")
        invalid = [
            ("../escape", tarfile.REGTYPE, b"bad"),
            ("/absolute", tarfile.REGTYPE, b"bad"),
            ("other/file", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/../escape", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/a\\b", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/C:escape", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/con.txt", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/trailing.", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/link", tarfile.SYMTYPE, b""),
            (PACKAGE + "/hardlink", tarfile.LNKTYPE, b""),
            (PACKAGE + "/pipe", tarfile.FIFOTYPE, b""),
            (PACKAGE + "/generated.pyc", tarfile.REGTYPE, b"bad"),
            (PACKAGE + "/__pycache__/data", tarfile.REGTYPE, b"bad"),
            valid,
            (PACKAGE + "/SAFE.py", tarfile.REGTYPE, b"bad"),
        ]
        for member in invalid:
            with self.subTest(member=member[:2]):
                bundle = self.fixture([valid, member])
                destination = self.root / "destination"
                with self.assertRaises(ValueError):
                    delivery.extract_artifact(bundle, destination)
                self.assertFalse(destination.exists())

    def test_implicit_case_colliding_directories_are_rejected(self):
        bundle = self.fixture([
            (PACKAGE + "/Upper/a.py", tarfile.REGTYPE, b"one"),
            (PACKAGE + "/upper/b.py", tarfile.REGTYPE, b"two"),
        ])
        with self.assertRaisesRegex(ValueError, "Case-colliding"):
            delivery.load_artifact(bundle)

    def test_file_directory_collision_is_rejected(self):
        bundle = self.fixture([
            (PACKAGE + "/parent", tarfile.REGTYPE, b"one"),
            (PACKAGE + "/parent/child.py", tarfile.REGTYPE, b"two"),
        ])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            delivery.load_artifact(bundle)

    def test_manifest_must_describe_exact_files_and_hashes(self):
        members = [(PACKAGE + "/safe.py", tarfile.REGTYPE, b"safe")]
        for files in (
            {PACKAGE + "/different.py": hashlib.sha256(b"safe").hexdigest()},
            {PACKAGE + "/safe.py": "0" * 64},
            {PACKAGE + "/safe.py": "0" * 64, PACKAGE + "/missing.py": "0" * 64},
        ):
            with self.subTest(files=files), self.assertRaisesRegex(
                ValueError, "member set or file hashes"
            ):
                delivery.load_artifact(self.fixture(members, manifest_files=files))

    def test_duplicate_manifest_keys_are_rejected(self):
        bundle = self.fixture([(PACKAGE + "/safe.py", tarfile.REGTYPE, b"safe")])
        manifest = bundle / "artifact.json"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace('"schema": 1', '"schema": 1, "schema": 1'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            delivery.load_artifact(bundle)

    def test_hash_matching_non_archive_has_concise_cli_failure(self):
        bundle = self.fixture([(PACKAGE + "/safe.py", tarfile.REGTYPE, b"safe")])
        (bundle / "artifact.tar.gz").write_bytes(b"not a tar archive")
        path = bundle / "artifact.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["archive_sha256"] = hashlib.sha256(b"not a tar archive").hexdigest()
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.cli("verify", "--bundle", bundle, expected=1)
        self.assertIn("Invalid artifact archive", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class CheckTests(Workspace):
    def setUp(self):
        super().setUp()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.data = self.inputs / "value.txt"
        self.data.write_text("0", encoding="utf-8")
        self.counter = self.root / "counter.txt"
        self.record = self.root / "receipt.json"
        self.runner = self.inputs / "runner.py"
        self.runner.write_text(
            "import pathlib, sys\n"
            "counter = pathlib.Path(sys.argv[1])\n"
            "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
            "print('stdout:' + sys.argv[3])\n"
            "print('stderr:captured', file=sys.stderr)\n"
            "sys.exit(int(pathlib.Path(sys.argv[2]).read_text()))\n",
            encoding="utf-8",
        )
        self.command = [sys.executable, str(self.runner), str(self.counter), str(self.data), "literal & ; spaces"]

    def check(self, *, expected=0, context="tool=one", command=None, inputs=None):
        selected = inputs if inputs is not None else [self.inputs]
        flags = ["check", "--record", self.record, "--context", context]
        for path in selected:
            flags.extend(["--input", path])
        return self.cli(*flags, "--", *(command or self.command), expected=expected)

    def count(self):
        return int(self.counter.read_text())

    def test_success_reuses_only_identical_command_inputs_and_context(self):
        first = self.check()
        self.assertEqual(first.stdout.splitlines(), ["stdout:literal & ; spaces"])
        self.assertIn("stderr:captured", first.stderr)
        second = self.check()
        self.assertIn("Reusing successful check", second.stderr)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(self.count(), 1)
        added = self.inputs / "new.txt"
        added.write_text("added", encoding="utf-8")
        self.check()
        self.assertEqual(self.count(), 2)
        added.write_text("changed", encoding="utf-8")
        self.check()
        self.assertEqual(self.count(), 3)
        added.unlink()
        self.check()
        self.assertEqual(self.count(), 4)
        self.check(context="tool=two")
        self.assertEqual(self.count(), 5)
        self.check(context="tool=two", command=[*self.command[:-1], "different"])
        self.assertEqual(self.count(), 6)

    def test_failed_check_is_recorded_propagated_and_never_reused(self):
        self.data.write_text("7", encoding="utf-8")
        self.check(expected=7)
        self.check(expected=7)
        self.assertEqual(self.count(), 2)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(record["returncode"], 7)
        self.assertEqual(record["stdout"].splitlines(), ["stdout:literal & ; spaces"])
        self.assertIn("stderr:captured", record["stderr"])
        self.data.write_text("0", encoding="utf-8")
        self.check()
        self.check()
        self.assertEqual(self.count(), 3)

    def test_generated_and_private_changes_are_excluded_unless_selected(self):
        private = self.inputs / "secrets"
        private.mkdir()
        secret = private / "data.txt"
        secret.write_text("first", encoding="utf-8")
        cache = self.inputs / "__pycache__"
        cache.mkdir()
        bytecode = cache / "generated.pyc"
        bytecode.write_bytes(b"first")
        self.check()
        secret.write_text("second", encoding="utf-8")
        bytecode.write_bytes(b"second")
        self.check()
        self.assertEqual(self.count(), 1)
        selected = [self.inputs, private, bytecode]
        self.check(inputs=selected)
        self.assertEqual(self.count(), 2)
        secret.write_text("third", encoding="utf-8")
        self.check(inputs=selected)
        self.assertEqual(self.count(), 3)
        bytecode.write_bytes(b"third")
        self.check(inputs=selected)
        self.assertEqual(self.count(), 4)

    def test_symlink_input_is_rejected_before_dispatch_even_when_excluded(self):
        self.symlink(self.inputs / "secrets", self.root / "elsewhere", directory=True)
        result = self.check(expected=1)
        self.assertIn("Links are not allowed", result.stderr)
        self.assertFalse(self.counter.exists())
        self.assertFalse(self.record.exists())

    def test_missing_inputs_do_not_dispatch(self):
        self.check(inputs=[self.root / "missing"], expected=1)
        self.assertFalse(self.counter.exists())
        self.assertFalse(self.record.exists())

    def test_malformed_or_interrupted_receipt_runs_instead_of_reusing(self):
        self.check()
        for count, invalid in enumerate(("{broken", "[]"), start=2):
            self.record.write_text(invalid, encoding="utf-8")
            self.check()
            self.assertEqual(self.count(), count)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        record["status"] = "running"
        self.record.write_text(json.dumps(record), encoding="utf-8")
        self.check()
        self.assertEqual(self.count(), 4)

    def test_input_changed_during_check_cannot_be_reused(self):
        command = [sys.executable, "-c",
                   "from pathlib import Path; p=Path('inputs/value.txt'); p.write_text(p.read_text()+'0')"]
        self.check(command=command)
        receipt = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertFalse(receipt["inputs_stable"])
        self.check(command=command)
        self.assertEqual(self.data.read_text(encoding="utf-8"), "000")

    def test_missing_executable_is_a_failed_receipt_not_a_retry(self):
        self.check(command=[str(self.root / "missing-executable")], expected=127)
        receipt = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["returncode"], 127)
        self.assertFalse(self.counter.exists())

    def test_duplicate_context_is_rejected_before_dispatch(self):
        self.cli("check", "--record", self.record, "--input", self.inputs,
                 "--context", "tool=one", "--context", "tool=two", "--", *self.command, expected=1)
        self.assertFalse(self.counter.exists())

class DockerWorkflowTests(Workspace):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "tools/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.repo / "tools/ha_smoke.py").write_text(
            'CORE_COMPONENTS = ("homeassistant", "http")\n'
            'BOOTSTRAP_COMPONENTS = (*CORE_COMPONENTS, "sensor")\n',
            encoding="utf-8",
        )
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (self.repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    def fake_docker(self, mode: str) -> Path:
        helper = self.root / "fake_docker.py"
        state = self.root / "docker_state.json"
        helper.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            f"state_path = Path({str(state)!r})\n"
            f"mode = {mode!r}\n"
            "data = json.loads(state_path.read_text()) if state_path.exists() else {'builds': 0, 'runs': 0, 'images': {}}\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['version']:\n"
            "    if mode == 'down':\n"
            "        sys.stderr.write('Cannot connect to the Docker daemon\\n')\n"
            "        raise SystemExit(1)\n"
            "    print('29.8.2')\n"
            "elif args[:2] == ['image', 'inspect']:\n"
            "    tag = args[-1]\n"
            "    if tag not in data['images']:\n"
            "        raise SystemExit(1)\n"
            "    print(data['images'][tag])\n"
            "elif args[:1] == ['build']:\n"
            "    data['builds'] += 1\n"
            "    data['last_build_args'] = args\n"
            "    tag = args[args.index('-t') + 1]\n"
            "    data['images'][tag] = 'sha256:' + 'b' * 64\n"
            "    state_path.write_text(json.dumps(data))\n"
            "elif args[:1] == ['run']:\n"
            "    data['runs'] += 1\n"
            "    data['last_run_args'] = args\n"
            "    state_path.write_text(json.dumps(data))\n"
            "    if '--serve' in args:\n"
            "        output_idx = args.index('--output') + 1 if '--output' in args else None\n"
            "        output_path = Path(args[output_idx]) if (output_idx and output_idx < len(args)) else None\n"
            "        print(json.dumps({'event': 'serving', 'url': 'http://127.0.0.1:8123', 'entry_id': 'test_entry'}), flush=True)\n"
            "        while True:\n"
            "            line = sys.stdin.readline()\n"
            "            if not line:\n"
            "                break\n"
            "            cmd = line.strip()\n"
            "            if cmd in {'stop', 'quit', 'exit'}:\n"
            "                if output_path is not None:\n"
            "                    output_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "                    output_path.write_text(json.dumps({'schema': 1, 'success': True, 'stopped_by': 'command'}), encoding='utf-8')\n"
            "                break\n"
            "    else:\n"
            "        print('CONTAINER_RAN')\n",
            encoding="utf-8",
        )
        if sys.platform == "win32":
            shim = self.root / "fake-docker.cmd"
            shim.write_text(f'@"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8")
        else:
            shim = self.root / "fake-docker"
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        return shim

    def test_unavailable_docker_fails_fast_with_explicit_user_notice(self):
        missing = self.cli("docker", "--repo", self.repo, "--docker-bin", self.root / "missing-docker", expected=2)
        self.assertIn("DOCKER_UNAVAILABLE", missing.stderr)
        down = self.cli("docker", "--repo", self.repo, "--docker-bin", self.fake_docker("down"), "--", "pytest", expected=2)
        self.assertIn("DOCKER_UNAVAILABLE", down.stderr)
        self.assertFalse((self.root / "docker_state.json").exists())

    def test_docker_image_builds_once_then_reuses_without_rebuilding(self):
        shim = self.fake_docker("up")
        first = self.cli("docker", "--repo", self.repo, "--docker-bin", shim)
        info1 = json.loads(first.stdout)
        self.assertEqual(info1["built"], "true")
        second = self.cli("docker", "--repo", self.repo, "--docker-bin", shim)
        info2 = json.loads(second.stdout)
        self.assertEqual(info2["built"], "false")
        self.assertEqual(info1["image"], info2["image"])
        ran = self.cli("docker", "--repo", self.repo, "--docker-bin", shim, "--", "pytest", "-q")
        self.assertIn("CONTAINER_RAN", ran.stdout)
        state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["builds"], 1)
        self.assertEqual(state["runs"], 1)
        self.assertIn("--network", state["last_run_args"])
        self.assertIn("none", state["last_run_args"])
        self.cli("docker", "--repo", self.repo, "--docker-bin", shim, "--", "python3", "tools/ha_smoke.py", "--serve")
        serve_state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        self.assertIn("-i", serve_state["last_run_args"])
        self.assertTrue(any(arg.startswith("127.0.0.1:") for arg in serve_state["last_run_args"]))
        self.assertTrue(any(arg.startswith("UPLUS_SMOKE_CONTAINER_SERVE_PORT=") for arg in serve_state["last_run_args"]))
        (self.repo / "tools/ha_smoke.py").write_text(
            'CORE_COMPONENTS = ("homeassistant", "http")\n'
            'BOOTSTRAP_COMPONENTS = (*CORE_COMPONENTS, "sensor")\n'
            '# changed scenario logic only\n',
            encoding="utf-8",
        )
        same_tag = json.loads(self.cli("docker", "--repo", self.repo, "--docker-bin", shim).stdout)
        self.assertEqual(same_tag["built"], "false")
        self.assertEqual(same_tag["image"], info1["image"])
        (self.repo / "tools/ha_smoke.py").write_text(
            'CORE_COMPONENTS = ("homeassistant", "http", "websocket_api")\n'
            'BOOTSTRAP_COMPONENTS = (*CORE_COMPONENTS, "sensor")\n',
            encoding="utf-8",
        )
        rebuilt = json.loads(self.cli("docker", "--repo", self.repo, "--docker-bin", shim).stdout)
        self.assertEqual(rebuilt["built"], "true")
        self.assertNotEqual(rebuilt["image"], info1["image"])
        build_state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        self.assertIn("SMOKE_COMPONENTS=homeassistant http websocket_api sensor", build_state["last_build_args"])

    def test_docker_command_normalizes_python_scripts_with_python3(self):
        shim = self.fake_docker("up")
        self.cli("docker", "--repo", self.repo, "--docker-bin", shim, "--", "tools/ha_smoke.py", "--serve")
        state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        cmd_index = state["last_run_args"].index("python3")
        self.assertEqual(state["last_run_args"][cmd_index + 1], "tools/ha_smoke.py")
        self.cli("docker", "--repo", self.repo, "--docker-bin", shim, "--", "python3", "tools/ha_smoke.py", "--serve")
        state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_run_args"].count("python3"), 1)
        self.cli("docker", "--repo", self.repo, "--docker-bin", shim, "--", "pytest", "-q")
        state = json.loads((self.root / "docker_state.json").read_text(encoding="utf-8"))
        self.assertNotIn("python3", state["last_run_args"])
        self.assertIn("pytest", state["last_run_args"])

    def test_docker_serve_lifecycle_regression(self):
        shim = self.fake_docker("up")
        bundle = self.root / "bundle"
        bundle.mkdir()
        output = self.root / "serve-receipt.json"

        # 1. Immediate EOF on stdin causes early exit without clean stop receipt
        eof_proc = subprocess.Popen(
            [
                sys.executable, str(TOOL), "docker", "--repo", str(self.repo),
                "--docker-bin", str(shim), "--", "tools/ha_smoke.py",
                "--bundle", str(bundle), "--output", str(output), "--serve",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        eof_stdout, _ = eof_proc.communicate(timeout=10)
        self.assertIn('"event": "serving"', eof_stdout)
        self.assertEqual(eof_proc.returncode, 0)
        self.assertFalse(output.exists(), "Immediate EOF must not produce clean shutdown receipt")

        # 2. Persistent stdin pipe stays alive until stop command
        with subprocess.Popen(
            [
                sys.executable, str(TOOL), "docker", "--repo", str(self.repo),
                "--docker-bin", str(shim), "--", "tools/ha_smoke.py",
                "--bundle", str(bundle), "--output", str(output), "--serve",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        ) as proc:
            # Bounded readiness
            ready_line = proc.stdout.readline()
            self.assertIn('"event": "serving"', ready_line)
            self.assertIsNone(proc.poll(), "Process must remain alive while stdin is held open")

            # Graceful stop protocol via stdin
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
        self.assertTrue(output.exists(), "Clean shutdown must write receipt")
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(receipt["success"])
        self.assertEqual(receipt["stopped_by"], "command")

if __name__ == "__main__":
    unittest.main()
