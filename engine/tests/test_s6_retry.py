"""Tests for safe retry of failed IMPLEMENT stage (S6.3 A2+A3) and Part 0."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_project, make_repository
from reoengine._data import Json, atomic_write
from reoengine.cli import main
from reoengine.config import Configuration, Engine, Limits, Profile, RoleConfig
from reoengine.fsm import (
    Pipeline,
    StageError,
    TransientStageError,
    is_transient_failure,
)
from reoengine.gitops import tree_hash
from reoengine.slots import WriterLockTimeout
from reoengine.state import RunState, Stage, Status


class S6RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-retry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repository(self.root)
        self.project = make_project(self.repo)

    def _create_pipeline(self, roles: dict[str, RoleConfig], adapter: FakeAdapter) -> Pipeline:
        cfg = Configuration(roles, Limits(), {"synthetic": Profile()}, {"test": self.project})
        pipeline = Pipeline(
            cfg,
            self.project,
            self.root / "run",
            self.root / "state",
            {Engine.CODEX: adapter},
            role_provider=lambda: roles,
        )
        adapter.worktree = pipeline.git.root
        return pipeline

    def test_1_quota_failure_resume_retry_succeeds(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)
        error_file = pipeline.directory / "error.json"
        self.assertTrue(error_file.exists())
        error_data = json.loads(error_file.read_text(encoding="utf-8"))
        self.assertEqual(error_data["stage"], "implement")
        self.assertIs(error_data["transient"], True)

        roles["coder"] = RoleConfig(Engine.CODEX, "coder")
        resumed = pipeline.resume(retry_failed=True)

        self.assertTrue((pipeline.directory / "checkpoints/implement-1.json").exists())
        self.assertNotEqual(resumed.status, Status.FAILED)
        retries_file = pipeline.directory / "retries.json"
        self.assertTrue(retries_file.exists())
        self.assertEqual(json.loads(retries_file.read_text(encoding="utf-8")), {"implement:1": 1})
        self.assertFalse(error_file.exists())

    def test_2_worktree_reset_on_retry(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)

        junk_file = pipeline.git.root / "junk.txt"
        junk_file.write_text("junk content\n", encoding="utf-8")
        tracked_file = pipeline.git.root / "unchanged.txt"
        tracked_file.write_text("tampered content\n", encoding="utf-8")
        self.assertTrue(junk_file.exists())
        self.assertEqual(tracked_file.read_text(encoding="utf-8"), "tampered content\n")

        pipeline.resume(retry_failed=True)

        self.assertFalse(junk_file.exists())
        self.assertEqual(tracked_file.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(pipeline.git.head(), pipeline.state.base_sha)
        diff = pipeline.git.diff(pipeline.state.base_sha)
        self.assertEqual(diff.changes, ())
        self.assertEqual(pipeline.state.tree_hash, tree_hash(pipeline.git.root, pipeline.git.exclude_from_worktree))

    def test_3_nontransient_failure_refuses_retry(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        original_perform = pipeline._perform

        def failing_perform(stage: Stage) -> dict[str, Json]:
            if stage == Stage.IMPLEMENT:
                raise StageError("boom")
            return original_perform(stage)

        with patch.object(pipeline, "_perform", side_effect=failing_perform):
            state = pipeline.start(make_inputs())

        self.assertEqual(state.status, Status.FAILED)
        error_file = pipeline.directory / "error.json"
        self.assertTrue(error_file.exists())
        error_data = json.loads(error_file.read_text(encoding="utf-8"))
        self.assertIs(error_data["transient"], False)

        junk_file = pipeline.git.root / "junk.txt"
        junk_file.write_text("junk content\n", encoding="utf-8")

        with self.assertRaises(StageError) as ctx:
            pipeline.resume(retry_failed=True)

        self.assertTrue(str(ctx.exception).startswith("resume refused:"))
        self.assertEqual(str(ctx.exception), "resume refused: failure is not transient")
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertTrue(junk_file.exists())
        self.assertFalse((pipeline.directory / "retries.json").exists())

    def test_4_resume_without_retry_failed_preserves_tree_and_failed_status(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)

        junk_file = pipeline.git.root / "junk.txt"
        junk_file.write_text("junk content\n", encoding="utf-8")

        resumed = pipeline.resume(retry_failed=False)
        self.assertEqual(resumed.status, Status.FAILED)
        self.assertTrue(junk_file.exists())
        self.assertFalse((pipeline.directory / "retries.json").exists())

    def test_5_retry_budget_exhausted(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)

        # 1st retry
        resumed1 = pipeline.resume(retry_failed=True)
        self.assertEqual(resumed1.status, Status.FAILED)
        retries_file = pipeline.directory / "retries.json"
        self.assertEqual(json.loads(retries_file.read_text(encoding="utf-8")), {"implement:1": 1})

        # 2nd retry
        resumed2 = pipeline.resume(retry_failed=True)
        self.assertEqual(resumed2.status, Status.FAILED)
        self.assertEqual(json.loads(retries_file.read_text(encoding="utf-8")), {"implement:1": 2})

        # 3rd retry exceeds budget of 2
        with self.assertRaises(StageError) as ctx:
            pipeline.resume(retry_failed=True)

        self.assertEqual(str(ctx.exception), "resume refused: implement retry budget exhausted (2)")
        self.assertEqual(json.loads(retries_file.read_text(encoding="utf-8")), {"implement:1": 2})

    def test_6_attempt_not_one_refused(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)

        pipeline._save(attempt=2)

        with self.assertRaises(StageError) as ctx:
            pipeline.resume(retry_failed=True)

        self.assertEqual(str(ctx.exception), "resume refused: retry-failed is supported only for the first implement attempt")

    def test_7_corrupt_retries_json_refused(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        state = pipeline.start(make_inputs())
        self.assertEqual(state.status, Status.FAILED)

        (pipeline.directory / "retries.json").write_text("{corrupt json", encoding="utf-8")

        with self.assertRaises(StageError) as ctx:
            pipeline.resume(retry_failed=True)

        self.assertEqual(str(ctx.exception), "resume refused: retries.json is corrupt")

    def test_8_cli_resume_retry_failed_passed_to_pipeline(self) -> None:
        config_dir = self.root / "config"
        (config_dir / "profiles").mkdir(parents=True)
        (config_dir / "projects").mkdir()
        roles_text = "".join(f'[roles.{name}]\nengine="codex"\nmodel="{name}"\nfallback=["{name}-alt"]\n'
                             for name in ("coder", "reader", "reviewer", "architect", "researcher"))
        (config_dir / "roles.toml").write_text(roles_text, encoding="utf-8")
        (config_dir / "limits.toml").write_text("max_fix_rounds=2\n", encoding="utf-8")
        (config_dir / "profiles/synthetic.toml").write_text('rules="Synthetic rule"\n', encoding="utf-8")
        (config_dir / "projects/synthetic.toml").write_text(
            f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
            'allowed_paths=["src/**", "tests/**"]\n[acceptance]\n'
            f'unit-tests={json.dumps([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])}\n',
            encoding="utf-8")

        run_dir = self.root / "runs" / "test-run"
        run_dir.mkdir(parents=True)
        atomic_write(run_dir / "launch.json", {"project": "synthetic"})
        (run_dir / "events.jsonl").touch()

        fake_state = RunState("test-run", status=Status.COMPLETED)
        out, err = io.StringIO(), io.StringIO()

        with patch.object(Pipeline, "resume", return_value=fake_state) as mock_resume:
            with patch("reoengine.cli.codex_rate_limits", side_effect=AssertionError("No real probes")):
                code = main(["resume", "test-run", "--retry-failed"],
                            root=self.root, adapters={Engine.CODEX: FakeAdapter()},
                            out=out, err=err)

        self.assertEqual(code, 0, err.getvalue())
        mock_resume.assert_called_once_with(remove_stale=False, retry_failed=True)

    def test_9_writer_lock_timeout_is_transient(self) -> None:
        roles = {name: RoleConfig(Engine.CODEX, name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        adapter = FakeAdapter()
        pipeline = self._create_pipeline(roles, adapter)

        original_perform = pipeline._perform

        def perform_with_lock_timeout(stage: Stage) -> dict[str, Json]:
            if stage == Stage.IMPLEMENT:
                raise WriterLockTimeout("synthetic slot timeout")
            return original_perform(stage)

        with patch.object(pipeline, "_perform", side_effect=perform_with_lock_timeout):
            state = pipeline.start(make_inputs())

        self.assertEqual(state.status, Status.FAILED)
        error_file = pipeline.directory / "error.json"
        self.assertTrue(error_file.exists())
        payload = json.loads(error_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage"], "implement")
        self.assertIs(payload["transient"], True)
        self.assertIs(is_transient_failure(TransientStageError("synthetic slot timeout")), True)


if __name__ == "__main__":
    unittest.main()
