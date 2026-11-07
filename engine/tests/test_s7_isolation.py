from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_pipeline, make_repository
from reoengine.config import IsolationConfig, Limits, load_limits
from reoengine.fsm import Status
from reoengine.gates import GateRequest, plant_canary, run_gates
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

        invalid = (
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
