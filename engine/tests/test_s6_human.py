from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, git_command, make_inputs, make_pipeline, make_project, make_repository
from reona._data import Json, atomic_write, loads, object_value, read_lines
from reona.adapters.base import AgentResult
from reona.cli import main
from reona.config import Configuration, Engine, Limits, RoleConfig
from dataclasses import replace
from reona.events import EventKind, sha256_file
from reona.fsm import Pipeline, RunInputs, StageError
from reona.schemas import validate_artifact
from reona.state import RunState, Stage, Status, load_state, save_state


class HumanApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-human-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_approve_awaiting_human_success(self) -> None:
        repo = make_repository(self.root)
        head_before = git_command(repo, "rev-parse", "HEAD").decode().strip()
        branch_before = git_command(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        log_before = git_command(repo, "log", "-1", "--oneline").decode().strip()

        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        state = pipeline.start(make_inputs())
        self.assertEqual(state.stage, Stage.ACCEPT)
        self.assertEqual(state.status, Status.AWAITING_HUMAN)

        result = pipeline.approve()
        self.assertEqual(result["status"], "completed")

        loaded = load_state(pipeline.directory)
        self.assertEqual(loaded.stage, Stage.DONE)
        self.assertEqual(loaded.status, Status.COMPLETED)

        result_path = pipeline.directory / "result.json"
        self.assertTrue(result_path.is_file())
        raw_result = loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(raw_result, dict):
            raise AssertionError("result.json must be a dict")
        validate_artifact("result", raw_result)
        self.assertEqual(raw_result["status"], "completed")

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        self.assertTrue(any(event.get("event") == EventKind.HUMAN_APPROVED.value for event in events))

        # Ensure source repository is intact: no commits, no branch changes
        head_after = git_command(repo, "rev-parse", "HEAD").decode().strip()
        branch_after = git_command(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        log_after = git_command(repo, "log", "-1", "--oneline").decode().strip()
        self.assertEqual(head_before, head_after)
        self.assertEqual(branch_before, branch_after)
        self.assertEqual(log_before, log_after)

        # Worktree remains intact
        self.assertTrue(pipeline.git.root.exists())
        self.assertTrue((pipeline.git.root / "src/value.py").is_file())

    def test_approve_refused_for_invalid_states(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        invalid_combinations = [
            (Stage.IMPLEMENT, Status.RUNNING),
            (Stage.GATES, Status.FAILED),
            (Stage.ACCEPT, Status.STOPPED),
            (Stage.DONE, Status.COMPLETED),
        ]
        for stage, status in invalid_combinations:
            with self.subTest(stage=stage.value, status=status.value):
                save_state(pipeline.directory, RunState(pipeline.state.run_id, stage=stage, status=status))
                with self.assertRaises(StageError):
                    pipeline.approve()
                persisted = load_state(pipeline.directory)
                self.assertEqual(persisted.stage, stage)
                self.assertEqual(persisted.status, status)

    def test_approve_refused_when_worktree_modified(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        self.assertEqual(load_state(pipeline.directory).status, Status.AWAITING_HUMAN)

        # Modify file in worktree so tree_hash differs
        (pipeline.git.root / "src/value.py").write_text("VALUE = 999\n", encoding="utf-8")

        with self.assertRaises(StageError) as context:
            pipeline.approve()
        self.assertIn("worktree differs from checkpoint tree hash", str(context.exception))

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_gates_tampered(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        # Tamper gates.json to passed = false
        atomic_write(pipeline.directory / "gates.json", {"passed": False, "checks": []})
        with self.assertRaises(StageError):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_review_tampered(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        # Tamper review.json with a high severity finding
        tampered_review: dict[str, Json] = {
            "approved": True,
            "summary": "tampered",
            "findings": [{"path": "src/value.py", "line": 1, "severity": "high", "text": "bad code"}],
        }
        atomic_write(pipeline.directory / "review.json", tampered_review)
        with self.assertRaises(StageError):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_binding_tampered(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        # Tamper binding.json
        tampered_binding: dict[str, Json] = {"input_digest": "corrupted", "risk_tier": "normal"}
        atomic_write(pipeline.directory / "review/binding.json", tampered_binding)
        with self.assertRaises(StageError):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_inputs_missing(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        self.assertEqual(load_state(pipeline.directory).status, Status.AWAITING_HUMAN)

        (pipeline.directory / "inputs.json").unlink()

        with self.assertRaisesRegex(StageError, "approve refused: run inputs missing"):
            pipeline.approve()

        fresh_pipeline = make_pipeline(self.root, repo, adapter)
        fresh_pipeline.inputs = RunInputs({})

        with self.assertRaises(StageError) as context:
            fresh_pipeline.approve()
        self.assertIn("approve refused: run inputs missing", str(context.exception))

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_reject_awaiting_human_and_reason_truncation(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        long_reason = "R" * 600
        result = pipeline.reject(long_reason)
        self.assertEqual(result["status"], "stopped")

        loaded = load_state(pipeline.directory)
        self.assertEqual(loaded.stage, Stage.ACCEPT)
        self.assertEqual(loaded.status, Status.STOPPED)

        result_path = pipeline.directory / "result.json"
        raw_result = loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(raw_result, dict):
            raise AssertionError("result.json must be a dict")
        validate_artifact("result", raw_result)
        self.assertEqual(raw_result["status"], "stopped")

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        reject_events = [ev for ev in events if ev.get("event") == EventKind.HUMAN_REJECTED.value]
        self.assertEqual(len(reject_events), 1)
        note = reject_events[0].get("note")
        if not isinstance(note, str):
            raise AssertionError("note must be a string")
        self.assertEqual(len(note), 500)
        self.assertEqual(note, "R" * 500)

    def test_reject_token_crossing_500_boundary_sanitized(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        token = "sk-" + "a" * 30
        reason = "x" * 480 + " " + token
        result = pipeline.reject(reason)
        self.assertEqual(result["status"], "stopped")

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        reject_events = [ev for ev in events if ev.get("event") == EventKind.HUMAN_REJECTED.value]
        self.assertEqual(len(reject_events), 1)
        note = reject_events[0].get("note")
        if not isinstance(note, str):
            raise AssertionError("note must be a string")
        self.assertNotIn(token, note)
        self.assertNotIn("sk-", note)
        self.assertEqual(note[480:], " [redacted]")

    def test_reject_validation_and_idempotence(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        for empty_reason in ("", "   ", "\t\n"):
            with self.subTest(reason=repr(empty_reason)), self.assertRaises(StageError):
                pipeline.reject(empty_reason)

        with self.assertRaises(StageError):
            pipeline.reject("bad\x00reason")

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

        # First reject succeeds
        pipeline.reject("First rejection")
        self.assertEqual(load_state(pipeline.directory).status, Status.STOPPED)

        # Repeated reject fails
        with self.assertRaises(StageError):
            pipeline.reject("Second rejection")

        # Approve after reject fails
        with self.assertRaises(StageError):
            pipeline.approve()

    def test_approve_refused_when_checkpoint_tampered(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        self.assertEqual(load_state(pipeline.directory).status, Status.AWAITING_HUMAN)

        gate_checkpoint = pipeline.directory / "checkpoints" / f"gates-{pipeline.state.attempt}.json"
        self.assertTrue(gate_checkpoint.is_file())
        raw_bytes = bytearray(gate_checkpoint.read_bytes())
        raw_bytes[-1] ^= 0x01
        gate_checkpoint.write_bytes(bytes(raw_bytes))

        with self.assertRaisesRegex(StageError, "approve refused: completed stage artifact changed or missing"):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_inputs_hash_changed(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        self.assertEqual(load_state(pipeline.directory).status, Status.AWAITING_HUMAN)

        with (pipeline.directory / "inputs.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")

        with self.assertRaisesRegex(StageError, "approve refused: run inputs changed"):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_approve_refused_when_exclusions_changed_in_inputs(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        self.assertEqual(load_state(pipeline.directory).status, Status.AWAITING_HUMAN)

        inputs_path = pipeline.directory / "inputs.json"
        raw_inputs = loads(inputs_path.read_text(encoding="utf-8"))
        if not isinstance(raw_inputs, dict):
            raise AssertionError("inputs.json must be a dict")
        inputs_data = object_value(raw_inputs, "inputs")
        inputs_data["exclude_from_worktree"] = [".env.tampered"]
        atomic_write(inputs_path, inputs_data)

        # Synchronize run_inputs hash in state so hash check passes and exclusions check is reached
        state = load_state(pipeline.directory)
        new_hashes = dict(state.input_hashes)
        new_hashes["run_inputs"] = sha256_file(inputs_path)
        save_state(pipeline.directory, replace(state, input_hashes=new_hashes))

        with self.assertRaisesRegex(StageError, "approve refused: exclude_from_worktree configuration changed"):
            pipeline.approve()

        persisted = load_state(pipeline.directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

    def test_reject_huge_reason_bounded(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        huge_reason = "Z" * 100000
        result = pipeline.reject(huge_reason)
        self.assertEqual(result["status"], "stopped")

        loaded = load_state(pipeline.directory)
        self.assertEqual(loaded.stage, Stage.ACCEPT)
        self.assertEqual(loaded.status, Status.STOPPED)

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        reject_events = [ev for ev in events if ev.get("event") == EventKind.HUMAN_REJECTED.value]
        self.assertEqual(len(reject_events), 1)
        note = reject_events[0].get("note")
        if not isinstance(note, str):
            raise AssertionError("note must be a string")
        self.assertLessEqual(len(note), 500)
        self.assertEqual(note, "Z" * 500)

    def test_resume_does_not_resurrect_after_approve_or_reject(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())

        # Test approve then resume
        pipeline.approve()
        calls_after_approve = len(adapter.calls)
        resumed = pipeline.resume()
        self.assertEqual(resumed.stage, Stage.DONE)
        self.assertEqual(resumed.status, Status.COMPLETED)
        self.assertEqual(len(adapter.calls), calls_after_approve)

        # Test reject then resume
        pipeline2 = make_pipeline(self.root / "case2", repo, adapter)
        pipeline2.start(make_inputs())
        pipeline2.reject("Rejected")
        calls_after_reject = len(adapter.calls)
        resumed2 = pipeline2.resume()
        self.assertEqual(resumed2.stage, Stage.ACCEPT)
        self.assertEqual(resumed2.status, Status.STOPPED)
        self.assertEqual(len(adapter.calls), calls_after_reject)


class HumanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-human-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repository(self.root)
        self.config = self.root / "config"
        (self.config / "profiles").mkdir(parents=True)
        (self.config / "projects").mkdir()
        roles = "".join(f'[roles.{name}]\nengine="codex"\nmodel="{name}"\nfallback=["{name}-alt"]\n'
                        for name in ("coder", "reader", "reviewer", "architect", "researcher"))
        (self.config / "roles.toml").write_text(roles, encoding="utf-8")
        (self.config / "limits.toml").write_text("max_fix_rounds=2\n", encoding="utf-8")
        (self.config / "profiles/synthetic.toml").write_text('rules="Synthetic rule"\n', encoding="utf-8")
        (self.config / "projects/synthetic.toml").write_text(
            f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
            'allowed_paths=["src/**", "tests/**"]\n[acceptance]\n'
            f'unit-tests={json.dumps([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])}\n',
            encoding="utf-8")
        inputs = make_inputs()
        atomic_write(self.root / "task.json", inputs.task)
        atomic_write(self.root / "plan.json", inputs.plan)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.fake = FakeAdapter()

    def cli(self, args: list[str]) -> int:
        with patch("reona.cli.codex_rate_limits", side_effect=AssertionError("No real agent probes")):
            return main(args, root=self.root, adapters={Engine.CODEX: self.fake}, out=self.out, err=self.err)

    def run_task(self) -> int:
        return self.cli(["run", "synthetic", "--task", str(self.root / "task.json"), "--plan", str(self.root / "plan.json")])

    def run_directory(self) -> Path:
        runs = [p for p in (self.root / "runs").iterdir() if p.is_dir()]
        if not runs:
            raise AssertionError("No run directory created")
        return runs[-1]

    def test_cli_approve_flow(self) -> None:
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        directory = self.run_directory()
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)

        code = self.cli(["approve", directory.name])
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(load_state(directory).status, Status.COMPLETED)
        self.assertEqual(load_state(directory).stage, Stage.DONE)

        stdout_val = self.out.getvalue()
        self.assertIn("completed", stdout_val)
        self.assertIn("worktree:", stdout_val)
        self.assertIn("manually", stdout_val)

        # Repeated approve fails
        self.err = io.StringIO()
        repeat_code = self.cli(["approve", directory.name])
        self.assertEqual(repeat_code, 1)
        self.assertIn("stage error:", self.err.getvalue())

    def test_cli_reject_flow(self) -> None:
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        directory = self.run_directory()
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)

        # Missing --reason is argument error (exit code 2)
        code_missing_reason = self.cli(["reject", directory.name])
        self.assertEqual(code_missing_reason, 2)

        # Empty reason is stage error (exit code 1)
        self.err = io.StringIO()
        code_empty_reason = self.cli(["reject", directory.name, "--reason", ""])
        self.assertEqual(code_empty_reason, 1)

        # Valid reject
        self.out, self.err = io.StringIO(), io.StringIO()
        code_reject = self.cli(["reject", directory.name, "--reason", "Synthetic CLI rejection"])
        self.assertEqual(code_reject, 0, self.err.getvalue())
        self.assertEqual(load_state(directory).status, Status.STOPPED)

        events = (directory / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("human_rejected", events)
        self.assertIn("Synthetic CLI rejection", events)

        # Repeated reject fails
        self.err = io.StringIO()
        code_repeat = self.cli(["reject", directory.name, "--reason", "Second reject"])
        self.assertEqual(code_repeat, 1)

        # Approve after reject fails
        self.err = io.StringIO()
        code_approve_after = self.cli(["approve", directory.name])
        self.assertEqual(code_approve_after, 1)

        # Resume after reject does not resurrect run
        code_resume = self.cli(["resume", directory.name])
        self.assertEqual(code_resume, 1)
        self.assertEqual(load_state(directory).status, Status.STOPPED)

    def test_cli_approve_refused_when_inputs_missing(self) -> None:
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        directory = self.run_directory()
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)

        (directory / "inputs.json").unlink()

        self.err = io.StringIO()
        self.out = io.StringIO()
        code = self.cli(["approve", directory.name])
        self.assertEqual(code, 1)
        err_output = self.err.getvalue()
        self.assertIn("stage error: approve refused: run inputs missing", err_output)
        self.assertNotIn("Traceback", err_output)
        persisted = load_state(directory)
        self.assertEqual(persisted.stage, Stage.ACCEPT)
        self.assertEqual(persisted.status, Status.AWAITING_HUMAN)

