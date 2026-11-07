"""Regression coverage for the user facing examples and language profiles."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reoengine.config import load_configuration, load_profile, load_project
from reoengine.schemas import validate_file


ROOT = Path(__file__).resolve().parents[2]


class B1ExamplesTest(unittest.TestCase):
    def test_documentation_examples_validate(self) -> None:
        with patch.dict(os.environ, {"EXAMPLE_REPO": str(ROOT)}):
            project = load_project(ROOT / "docs" / "example-project.toml")
        self.assertEqual(project.profile, "default")
        self.assertIn("unit-tests", project.acceptance)
        validate_file("task", ROOT / "docs" / "example-task.json")
        validate_file("plan", ROOT / "docs" / "example-plan.json")

    def test_language_profiles_load_with_checks(self) -> None:
        python = load_profile(ROOT / "config" / "profiles" / "python.toml")
        node = load_profile(ROOT / "config" / "profiles" / "node.toml")
        self.assertEqual(python.checks, (("python", "-m", "unittest", "discover"),))
        self.assertEqual(node.checks, (("npm", "test"),))
        with tempfile.TemporaryDirectory(prefix="reoengine-b1-config-") as temporary:
            config = Path(temporary)
            (config / "profiles").mkdir()
            (config / "projects").mkdir()
            (config / "roles.toml").write_text(
                '[roles.coder]\nengine = "codex"\nmodel = "coder"\n', encoding="utf-8")
            shutil.copyfile(ROOT / "config" / "limits.toml", config / "limits.toml")
            shutil.copyfile(ROOT / "config" / "profiles" / "python.toml",
                            config / "profiles" / "python.toml")
            shutil.copyfile(ROOT / "config" / "profiles" / "node.toml",
                            config / "profiles" / "node.toml")
            for name in ("python", "node"):
                (config / "projects" / f"{name}.toml").write_text(
                    f'repo = "{config.as_posix()}"\nprofile = "{name}"\n', encoding="utf-8")
            loaded = load_configuration(config)
        self.assertEqual(loaded.projects["python"].profile, "python")
        self.assertEqual(loaded.projects["node"].profile, "node")

    def test_hello_world_runs_offline_to_human_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reoengine-b1-") as temporary:
            directory = Path(temporary) / "demo"
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(ROOT / "examples" / "hello-world" / "setup.py"),
                 "--directory", str(directory)],
                cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("end_state=awaiting_human", result.stdout)
        self.assertLess(elapsed, 60)


if __name__ == "__main__":
    unittest.main()
