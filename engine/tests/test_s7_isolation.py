from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_pipeline, make_repository
from reoengine.config import IsolationConfig, Limits, load_limits
from reoengine.fsm import Status
from reoengine.doctor import diagnose
from reoengine.procs import ProcessResult
from reoengine.gates import GateRequest, build_restricted_env, plant_canary, run_gates
from reoengine.gitops import Git
from reoengine.schemas import validate_artifact


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="example-isolation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_load_limits_isolation_defaults_and_validation(self) -> None:
        config = self.root / "limits.toml"
        config.write_text("max_slots = 8\n", encoding="utf-8")
        self.assertEqual(load_limits(config).isolation, IsolationConfig())

        config.write_text('[isolation]\nmode = "wrapper"\nwrapper = ["python", "-c", "pass"]\n', encoding="utf-8")
        limits = load_limits(config)
        self.assertEqual(limits.isolation.mode, "wrapper")
        self.assertEqual(limits.isolation.wrapper, ("python", "-c", "pass"))
        self.assertEqual(limits.review_max_diff_lines, 5000)
        config.write_text("review_full_diff_lines = 400\nreview_max_diff_lines = 5000\n", encoding="utf-8")
        self.assertEqual(load_limits(config).review_max_diff_lines, 5000)

        invalid = (
            "review_max_diff_lines = 0\n",
            "review_full_diff_lines = 501\nreview_max_diff_lines = 500\n",
            '[isolation]\nmode = "bad"\n',
            '[isolation]\nmode = "wrapper"\n',
            '[isolation]\nmode = "wrapper"\nwrapper = []\n',
            '[isolation]\nmode = "env"\nwrapper = ["", "arg"]\n',
            '[isolation]\nmode = "env"\nwrapper = ["bad\\u0000arg"]\n',
            '[isolation]\nunknown = true\n',
        )
        for source in invalid:
            with self.subTest(source=source):
                config.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_limits(config)

    def test_restricted_path_filters_relative_and_worktree_entries(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        system = self.root / "system-bin"
        system.mkdir()
        source = {"PATH": os.pathsep.join(("relative/bin", str(worktree / "bin"), str(system)))}
        env = build_restricted_env(source=source, exclude_dirs=(worktree,))
        self.assertEqual(env["PATH"], str(system))
        self.assertEqual(build_restricted_env(source={"PATH": "relative/bin"},
                                              exclude_dirs=(worktree,))["PATH"], "")

    @unittest.skipUnless(os.name == "nt", "Windows path comparisons are case insensitive")
    def test_restricted_path_exclusion_is_case_insensitive_on_windows(self) -> None:
        worktree = self.root / "WorkTree"
        worktree.mkdir()
        variant = str(worktree).swapcase()
        self.assertEqual(build_restricted_env(source={"PATH": variant},
                                              exclude_dirs=(worktree,))["PATH"], "")

    def test_wrapper_executes_check_and_gates_artifact_records_outcomes(self) -> None:
        repo = make_repository(self.root)
        git = Git(repo)
        base = git.head()
        (repo / "src/value.py").write_text("VALUE = 1\n", encoding="utf-8")
        plant_canary(git)
        marker = self.root / "wrapper-marker.txt"
        wrapper_code = (
            "from pathlib import Path; import subprocess,sys; "
            "Path(sys.argv[1]).write_text(sys.argv[2]); "
            "sys.exit(subprocess.call(sys.argv[3:]))"
        )
        request = GateRequest(
            base, ("src/**",), ((sys.executable, "-c", "pass"),), tdd_gate=False,
            tdd_gate_reason="synthetic tests", isolation_mode="wrapper",
            isolation_wrapper=(sys.executable, "-c", wrapper_code, str(marker), "{worktree}"),
        )
        report = run_gates(git, request, self.root / "gates.json")
        self.assertTrue(marker.is_file())
        self.assertEqual(os.path.realpath(marker.read_text(encoding="utf-8")), os.path.realpath(repo))
        artifact = json.loads((self.root / "gates.json").read_text(encoding="utf-8"))
        validate_artifact("gates", artifact)
        self.assertEqual(artifact["isolation"], "wrapper")
        project_check = next(check for check in artifact["checks"] if check["name"] == "project:0")
        self.assertEqual(project_check["outcome"], "pass")
        self.assertTrue(report.passed)

    def test_doctor_reports_isolation_mode_and_wrapper_availability(self) -> None:
        config = self.root / "config"
        config.mkdir()
        config.joinpath("limits.toml").write_text("[isolation]\nmode = \"env\"\n", encoding="utf-8")
        probe = lambda argv: ProcessResult(0, "version", "", 0.0)
        with patch("reoengine.doctor._validate_neutral_directory"):
            diagnostics = diagnose(config, self.root / "state", home=self.root,
                                  neutral_directory=self.root, probe=probe, roles_file=self.root / "missing.toml")
        self.assertTrue(any(d.name == "isolation" and d.level == "warning" and
                            d.detail == "checks run with environment filtering only; see docs/ISOLATION.md"
                            for d in diagnostics))
        config.joinpath("limits.toml").write_text(
            '[isolation]\nmode = "wrapper"\nwrapper = ["reoengine-missing-wrapper-xyz"]\n',
            encoding="utf-8")
        with patch("reoengine.doctor._validate_neutral_directory"):
            diagnostics = diagnose(config, self.root / "state", home=self.root,
                                  neutral_directory=self.root, probe=probe, roles_file=self.root / "missing.toml")
        self.assertTrue(any(d.name == "isolation" and d.level == "error" and
                            d.detail == "wrapper executable not found: reoengine-missing-wrapper-xyz"
                            for d in diagnostics))

    def test_env_only_result_warns_about_isolation_and_links(self) -> None:
        repo = make_repository(self.root)
        target = self.root / "host-directory"
        target.mkdir()
        pipeline = make_pipeline(self.root, repo, FakeAdapter())
        pipeline.project = replace(pipeline.project, links={"vendor": target})
        result = pipeline._result(Status.FAILED)
        validate_artifact("result", result)
        self.assertIn(
            "checks ran with environment filtering only (no network/filesystem isolation)",
            result["warnings"],
        )
        self.assertIn("link vendor gives the agent write access to a host directory", result["warnings"])


if __name__ == "__main__":
    unittest.main()
