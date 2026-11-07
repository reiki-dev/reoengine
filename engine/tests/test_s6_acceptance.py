from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_project, make_repository
from reoengine._data import atomic_write, loads, object_value, read_lines
from reoengine.config import Configuration, Effort, Engine, Limits, Profile, RoleConfig
from reoengine.events import EventKind
from reoengine.fsm import Pipeline, RunInputs, StageError
from reoengine.preflight import metrics, preflight
from reoengine.state import Stage, Status
from reoengine.usage import TaskOutcome, TaskOutcomeLog, TokenUsage, UsageRecord


class AcceptanceAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-acc-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_unaccepted_run_with_successful_plan_not_in_metrics(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter(mode="bad")
        pipeline = make_pipeline(self.root, repo, adapter)
        # Omit plan so architect is invoked and records usage outcome="accepted"
        inputs = RunInputs(make_inputs().task, None, False)
        pipeline.start(inputs)

        outcomes_log = TaskOutcomeLog(pipeline.state_directory)
        accepted_runs = {record.run_id for record in outcomes_log.read_all()}
        self.assertNotIn(pipeline.state.run_id, accepted_runs)

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        self.assertFalse(any(event.get("event") == EventKind.TASK_ACCEPTED.value for event in events))

        usage_records = pipeline.usage.read_days()
        run_records = [r for r in usage_records if r.run_id == pipeline.state.run_id]
        self.assertTrue(any(r.outcome == "accepted" and r.role == "architect" for r in run_records))
        self.assertTrue(any(r.outcome == "gate_failed" for r in run_records))

        stats = metrics(run_records, outcomes_log.read_all())
        self.assertEqual(stats.runs, 1)
        self.assertEqual(stats.accepted, 0)
        self.assertIsNone(stats.tokens_per_accepted)

    def test_successful_run_records_acceptance_only_after_approve(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter(mode="good")
        pipeline = make_pipeline(self.root, repo, adapter)
        state = pipeline.start(make_inputs())
        self.assertEqual(state.stage, Stage.ACCEPT)
        self.assertEqual(state.status, Status.AWAITING_HUMAN)

        outcomes_log = TaskOutcomeLog(pipeline.state_directory)
        outcomes = outcomes_log.read_all()
        self.assertEqual(outcomes, [])
        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        self.assertFalse(any(event.get("event") == EventKind.TASK_ACCEPTED.value for event in events))

        pipeline.approve()
        outcomes = outcomes_log.read_all()
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome.run_id, pipeline.state.run_id)
        self.assertEqual(outcome.attempt, 1)
        self.assertEqual(metrics(pipeline.usage.read_days(), outcomes_log.read_all()).accepted, 1)

        events = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        accepted_events = [e for e in events if e.get("event") == EventKind.TASK_ACCEPTED.value]
        self.assertEqual(len(accepted_events), 1)

        with self.assertRaises(StageError):
            pipeline.approve()
        self.assertEqual(len(outcomes_log.read_all()), 1)
        events_after_repeat = [object_value(line, "event") for line in read_lines(pipeline.events.path)]
        self.assertEqual(sum(e.get("event") == EventKind.TASK_ACCEPTED.value for e in events_after_repeat), 1)

    def test_tokens_per_accepted_calculation(self) -> None:
        now = datetime.now(timezone.utc)
        record1 = UsageRecord(
            "run-1", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 50, 0), 100, 0, "accepted", 1, ts=now,
        )
        record2 = UsageRecord(
            "run-2", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(200, 0, 100, 0), 100, 0, "accepted", 1, ts=now + timedelta(seconds=1),
        )
        record3 = UsageRecord(
            "run-3", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(50, 0, 20, 0), 100, 0, "gate_failed", 1, ts=now + timedelta(seconds=2),
        )
        all_records = [record1, record2, record3]
        accepted_outcomes = [
            TaskOutcome("run-1", 1, now),
            TaskOutcome("run-2", 1, now + timedelta(seconds=1)),
        ]

        stats = metrics(all_records, accepted_outcomes)
        self.assertEqual(stats.runs, 3)
        self.assertEqual(stats.accepted, 2)
        self.assertEqual(stats.tokens_per_accepted, 520 / 2)

    def test_first_rate_attempt_one_vs_two(self) -> None:
        now = datetime.now(timezone.utc)
        run_a_records = [
            UsageRecord(
                "run-a", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
                TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1, ts=now,
            ),
        ]
        outcome_a = TaskOutcome("run-a", 1, now)
        stats_a = metrics(run_a_records, [outcome_a])
        self.assertEqual(stats_a.first_rate, 1.0)

        run_b_records = [
            UsageRecord(
                "run-b", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
                TokenUsage(100, 0, 10, 0), 10, 0, "gate_failed", 1, ts=now,
            ),
            UsageRecord(
                "run-b", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
                TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 2, ts=now + timedelta(seconds=1),
            ),
        ]
        outcome_b = TaskOutcome("run-b", 2, now + timedelta(seconds=1))
        stats_b = metrics(run_b_records, [outcome_b])
        self.assertEqual(stats_b.first_rate, 0.0)

        combined_records = [*run_a_records, *run_b_records]
        combined_outcomes = [outcome_a, outcome_b]
        stats_combined = metrics(combined_records, combined_outcomes)
        self.assertEqual(stats_combined.runs, 2)
        self.assertEqual(stats_combined.accepted, 2)
        self.assertEqual(stats_combined.first_rate, 0.5)

    def test_missing_journal_graceful_defaults(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            UsageRecord(
                "run-old", Stage.PLAN, "architect", Engine.AGY, "claude-opus", Effort.HIGH,
                TokenUsage(100, 0, 50, 0), 10, 0, "accepted", 1, ts=now,
            ),
        ]
        stats = metrics(records)
        self.assertEqual(stats.runs, 1)
        self.assertEqual(stats.accepted, 0)
        self.assertEqual(stats.first_rate, 0.0)
        self.assertIsNone(stats.tokens_per_accepted)

        stats_empty = metrics(records, [])
        self.assertEqual(stats_empty.accepted, 0)
        self.assertEqual(stats_empty.first_rate, 0.0)
        self.assertIsNone(stats_empty.tokens_per_accepted)

    def test_approve_and_reject_do_not_create_new_acceptance_records(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter(mode="good")
        state_dir = self.root / "state"

        project = make_project(repo)
        roles = {name: RoleConfig(Engine.CODEX, name) for name in
                 ("coder", "reader", "reviewer", "researcher", "architect")}
        cfg = Configuration(roles, Limits(), {"synthetic": Profile()}, {"test": project})

        p1 = Pipeline(cfg, project, self.root / "runs/run1", state_dir, {Engine.CODEX: adapter})
        adapter.worktree = p1.git.root
        p1.start(make_inputs())

        outcomes_log = TaskOutcomeLog(state_dir)
        self.assertEqual(len(outcomes_log.read_all()), 0)

        p1.approve()
        self.assertEqual(len(outcomes_log.read_all()), 1)

        p2 = Pipeline(cfg, project, self.root / "runs/run2", state_dir, {Engine.CODEX: adapter})
        adapter.worktree = p2.git.root
        p2.start(make_inputs())
        self.assertEqual(len(outcomes_log.read_all()), 1)

        p2.reject("synthetic rejection")
        self.assertEqual(len(outcomes_log.read_all()), 1)

    def test_rejected_five_agent_run_has_no_accepted_metric(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter(mode="good")
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(RunInputs(make_inputs().task, None, True))
        self.assertEqual(len(adapter.calls), 5)
        pipeline.reject("synthetic rejection")
        outcomes = TaskOutcomeLog(pipeline.state_directory).read_all()
        run_records = [record for record in pipeline.usage.read_days()
                       if record.run_id == pipeline.state.run_id]
        self.assertEqual(outcomes, [])
        self.assertEqual(metrics(run_records, outcomes).accepted, 0)

    def test_acceptance_record_fields_and_no_secrets_or_paths(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter(mode="good")
        pipeline = make_pipeline(self.root, repo, adapter)
        pipeline.start(make_inputs())
        pipeline.approve()

        journal_path = pipeline.state_directory / "task-outcomes.jsonl"
        self.assertTrue(journal_path.is_file())
        raw_lines = read_lines(journal_path)
        self.assertEqual(len(raw_lines), 1)

        raw_line = raw_lines[0]
        if not isinstance(raw_line, dict):
            raise AssertionError("journal line must be a dict")

        allowed_fields = {"run_id", "ts", "attempt", "model_set"}
        self.assertTrue(set(raw_line.keys()).issubset(allowed_fields))

        run_id = raw_line.get("run_id")
        if not isinstance(run_id, str):
            raise AssertionError("run_id must be a string")
        self.assertNotIn("/", run_id)
        self.assertNotIn("\\", run_id)

        ts_str = raw_line.get("ts")
        if not isinstance(ts_str, str):
            raise AssertionError("ts must be a string")
        parsed_ts = datetime.fromisoformat(ts_str)
        self.assertIsNotNone(parsed_ts.tzinfo)

        attempt = raw_line.get("attempt")
        if not isinstance(attempt, int):
            raise AssertionError("attempt must be an int")
        self.assertGreaterEqual(attempt, 1)

        model_set = raw_line.get("model_set")
        if model_set is not None:
            if not isinstance(model_set, list):
                raise AssertionError("model_set must be a list")
            for item in model_set:
                if not isinstance(item, dict):
                    raise AssertionError("model_set element must be a dict")
                self.assertEqual(set(item.keys()), {"role", "engine", "model", "effort"})
                for val in item.values():
                    if not isinstance(val, str):
                        raise AssertionError("model_set values must be strings")
                    self.assertNotIn("://", val)
                    self.assertFalse(val.startswith("/"))
                    self.assertFalse(len(val) >= 2 and val[0].isalpha() and val[1] == ":")

        line_str = journal_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), line_str)
        self.assertNotIn(str(pipeline.directory), line_str)

    def test_concurrent_append_same_run_id_records_once(self) -> None:
        state_dir = self.root / "state"
        log1 = TaskOutcomeLog(state_dir)
        log2 = TaskOutcomeLog(state_dir)
        outcome = TaskOutcome("concurrent-run", 1)
        barrier = threading.Barrier(2)
        results: list[bool] = []
        lock = threading.Lock()

        def worker(log: TaskOutcomeLog) -> None:
            barrier.wait(timeout=5)
            res = log.append(outcome)
            with lock:
                results.append(res)

        t1 = threading.Thread(target=worker, args=(log1,))
        t2 = threading.Thread(target=worker, args=(log2,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(results), [False, True])
        outcomes = log1.read_all()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].run_id, "concurrent-run")
        raw_lines = read_lines(log1.path)
        self.assertEqual(len(raw_lines), 1)

    def test_corrupt_line_in_task_outcomes_append_succeeds_and_read_all_raises(self) -> None:
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        outcomes_log = TaskOutcomeLog(state_dir)
        outcomes_log.path.write_text("{\"corrupted_json\": true\nnot a json line\n", encoding="utf-8")

        outcome = TaskOutcome("fresh-run-1", 1)
        self.assertTrue(outcomes_log.append(outcome))
        self.assertFalse(outcomes_log.append(outcome))

        with self.assertRaises(ValueError):
            outcomes_log.read_all()

    def test_preflight_with_corrupt_task_outcomes_does_not_fail_and_skips_tuning(self) -> None:
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        outcomes_log = TaskOutcomeLog(state_dir)
        outcomes_log.path.write_text("corrupted journal line\n", encoding="utf-8")

        initial_tuning = {"roles": {"coder": {"effort": "low"}}}
        tuning_path = state_dir / "tuning.json"
        atomic_write(tuning_path, initial_tuning)

        roles = {name: RoleConfig(Engine.CODEX, name) for name in
                 ("coder", "reader", "reviewer", "researcher", "architect")}
        cfg = Configuration(roles, Limits(), {"synthetic": Profile()}, {})
        runs_dir = self.root / "runs"
        now = datetime.now(timezone.utc)

        lines = preflight(cfg, state_dir, runs_dir, apply=True, now=now)

        self.assertIn("acceptance log is corrupted: tuning skipped", lines)
        current_tuning = loads(tuning_path.read_text(encoding="utf-8"))
        self.assertEqual(current_tuning, initial_tuning)
        tuning_log = state_dir / "tuning-log.jsonl"
        self.assertFalse(tuning_log.exists())
