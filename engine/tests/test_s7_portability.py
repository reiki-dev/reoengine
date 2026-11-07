from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reoengine.adapters.agy import AgyAdapter
from reoengine.config import load_project
from reoengine.paths import neutral_path
from reoengine.procs import _environ_has_process_token, _parse_proc_stat


class ProcStatParserTests(unittest.TestCase):
    def test_parses_comm_with_spaces_and_parenthesis(self) -> None:
        self.assertEqual(_parse_proc_stat("123 (worker ) with spaces) S 45 7 8 9"), (123, 45))

    def test_process_token_parser_requires_exact_environment_entry(self) -> None:
        self.assertTrue(_environ_has_process_token(b"A=1\0REOENGINE_PROCESS_TOKEN=abc123\0B=2", "abc123"))
        self.assertFalse(_environ_has_process_token(b"REOENGINE_PROCESS_TOKEN=abc1234\0", "abc123"))
        self.assertFalse(_environ_has_process_token(b"PREFIX_REOENGINE_PROCESS_TOKEN=abc123\0", "abc123"))


class ConfigurationVariableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project_file = self.directory / "project.toml"

    def load(self, text: str):
        text = text.replace(chr(92) + "$" + "{", "$" + "{")
        self.project_file.write_text(text, encoding="utf-8")
        return load_project(self.project_file)

    def test_expands_repo_root_home_environment_links_and_argv(self) -> None:
        home = self.directory / "home"
        repo = self.directory / "repo"
        (repo / "node_modules").mkdir(parents=True)
        with patch.dict(os.environ, {"PORTABILITY_REPO": str(repo)}), patch("pathlib.Path.home", return_value=home):
            project = self.load('''repo = "\${ENV:PORTABILITY_REPO}"
profile = "default"
checks = [["\${HOME}/bin/check", "\${REOENGINE_ROOT}/arg", "$UNTOUCHED"]]
[links]
node_modules = "\${ENV:PORTABILITY_REPO}/node_modules"
[acceptance]
check = ["\${HOME}/bin/accept", "\${REOENGINE_ROOT}/accept"]
''')
        self.assertEqual(project.repo, repo)
        self.assertEqual(project.links["node_modules"], repo / "node_modules")
        self.assertEqual(Path(project.checks[0][0]), home / "bin" / "check")
        self.assertEqual(Path(project.checks[0][1]), Path(__file__).resolve().parents[2] / "arg")
        self.assertEqual(project.checks[0][2], "$UNTOUCHED")
        self.assertEqual(Path(project.acceptance["check"][0]), home / "bin" / "accept")

    def test_missing_environment_names_variable_and_config_path_without_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as error:
                self.load('repo = "\${ENV:PORTABILITY_MISSING}"\nprofile = "default"\n')
        self.assertIn(str(self.project_file), str(error.exception))
        self.assertIn("PORTABILITY_MISSING", str(error.exception))

    def test_relative_path_is_rejected_after_expansion(self) -> None:
        with patch.dict(os.environ, {"PORTABILITY_REPO": "relative/repo"}):
            with self.assertRaisesRegex(ValueError, "expected absolute path"):
                self.load('repo = "\${ENV:PORTABILITY_REPO}"\nprofile = "default"\n')


class PlatformDefaultTests(unittest.TestCase):
    def test_neutral_directory_priority_chain(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            base = Path(temporary)
            home = base / "home"
            local = base / "local"
            xdg = base / "xdg"
            override = base / "override"
            cases = (
                ({"REOENGINE_NEUTRAL_DIR": str(override), "LOCALAPPDATA": str(local), "XDG_STATE_HOME": str(xdg)}, override),
                ({"LOCALAPPDATA": str(local), "XDG_STATE_HOME": str(xdg)}, local / "reoengine" / "neutral"),
                ({"XDG_STATE_HOME": str(xdg)}, xdg / "reoengine" / "neutral"),
                ({}, home / ".local" / "state" / "reoengine" / "neutral"),
            )
            for environment, expected in cases:
                with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                        patch("pathlib.Path.home", return_value=home):
                    self.assertEqual(neutral_path(), expected)

    def test_agy_default_command_matches_platform(self) -> None:
        with patch("reoengine.adapters.agy.os.name", "nt"):
            self.assertEqual(AgyAdapter().command, ("agy.exe",))
        with patch("reoengine.adapters.agy.os.name", "posix"):
            self.assertEqual(AgyAdapter().command, ("agy",))


if __name__ == "__main__":
    unittest.main()
