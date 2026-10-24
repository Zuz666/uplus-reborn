"""Native CLI safety regressions; bootstrap the locked tool with delivery.py lint."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TOOL = Path(__file__).with_name("delivery.py").resolve()
LOCKED_RUFF = next(
    package["version"]
    for package in tomllib.loads(TOOL.parent.parent.joinpath("uv.lock").read_text(encoding="utf-8"))["package"]
    if package["name"] == "ruff"
)


class NativeLintSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        for name in ("custom_components", "tests", "tools"):
            (self.repo / name).mkdir()
        self.lock = self.repo / "uv.lock"
        self.lock.write_text(
            f'[[package]]\nname = "ruff"\nversion = "{LOCKED_RUFF}"\n', encoding="utf-8",
        )
        self.config = self.repo / "pyproject.toml"
        self.config.write_text('[tool.ruff.lint]\nselect = ["F"]\n', encoding="utf-8")
        self.source = self.repo / "tools" / "example.py"
        self.source.write_text("import os\n", encoding="utf-8")
        self.env = dict(os.environ, UV_OFFLINE="true")

    def check(self, **environment):
        result = subprocess.run(
            [sys.executable, str(TOOL), "lint", "--repo", str(self.repo)],
            capture_output=True, text=True, encoding="utf-8",
            env={**self.env, **environment}, check=False,
        )
        return result

    def test_fix_only_cannot_modify_sources_or_hide_errors(self):
        self.config.write_text(
            '[tool.ruff]\nfix-only = true\n[tool.ruff.lint]\nselect = ["F"]\n',
            encoding="utf-8",
        )
        before = self.source.read_bytes()
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("F401", result.stdout)
        self.assertEqual(self.source.read_bytes(), before)

    def test_output_file_environment_cannot_overwrite_source(self):
        before = self.source.read_bytes()
        result = self.check(RUFF_OUTPUT_FILE=str(self.source))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("F401", result.stdout)

    def test_uv_dotenv_cannot_inject_a_source_output_file(self):
        dotenv = self.repo / "unsafe.env"
        dotenv.write_text('RUFF_OUTPUT_FILE=tools/example.py\n', encoding="utf-8")
        before = self.source.read_bytes()
        result = self.check(UV_ENV_FILE=str(dotenv))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("F401", result.stdout)

    def test_override_cannot_replace_an_unavailable_locked_version(self):
        self.lock.write_text(
            '[[package]]\nname = "ruff"\nversion = "9999.0.0"\n', encoding="utf-8",
        )
        override = self.repo / "override.txt"
        override.write_text(f"ruff=={LOCKED_RUFF}\n", encoding="utf-8")
        self.source.write_text("value = 1\n", encoding="utf-8")
        result = self.check(UV_OVERRIDE=str(override))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_uv_working_directory_cannot_select_a_different_checkout(self):
        other = self.repo / "other"
        other.mkdir()
        for name in ("custom_components", "tests", "tools"):
            (other / name).mkdir()
        shutil.copyfile(self.config, other / "pyproject.toml")
        for variable in ("UV_WORKING_DIR", "UV_WORKING_DIRECTORY"):
            with self.subTest(variable=variable):
                result = self.check(**{variable: str(other)})
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("F401", result.stdout)
                self.assertIn("example.py", result.stdout)
