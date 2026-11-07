from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))

from pipeline import FakeAdapter, make_inputs, make_repository
from reoengine._data import atomic_write, loads, object_value, read_lines
from reoengine.adapters.base import AgentResult
from reoengine.adapters.codex import CodexAdapter
from reoengine.cli import main
from reoengine.config import (Configuration, Effort, Engine, Limits, Profile, RoleConfig,
                          apply_runtime_tuning, load_configuration)
from reoengine.doctor import diagnose
from reoengine.events import LimitRemaining
from reoengine.factory import make_adapters
from reoengine.fsm import Pipeline
from reoengine.gc import collect_failures
from reoengine.jsonout import extract_json
from reoengine.gitops import Git
from reoengine.preflight import metrics, preflight
from reoengine.procs import ProcessResult
from reoengine.state import RunState, Stage, Status, load_state, save_state
from reoengine.usage import TaskOutcome, TaskOutcomeLog, TokenUsage, UsageLog, UsageRecord

ENGINE = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


class TemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE, prefix="s3-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class JsonTests(unittest.TestCase):
    def test_extraction(self) -> None:
        cases = [('{"ok":true}', {"ok": True}),
                 ('leak\n<<<JSON_BEGIN>>>\n{"ok":true}\n<<<JSON_END>>> tail', {"ok": True}),
                 ('thinking...\n{"nested":{"text":"brace } and \\\" quote"}}', {"nested": {"text": 'brace } and " quote'}}),
                 ('{"old":1} {"last":{"x":2}} broken {invalid}', {"last": {"x": 2}}),
                 ('<<<JSON_BEGIN>>>{"old":1}<<<JSON_END>>> leak <<<JSON_BEGIN>>>{"last":2}<<<JSON_END>>>', {"last": 2}),
                 ('{"ok":1} {"bad": {"nested":1},}', {"ok": 1})]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_json(text), expected)
        for text in ("no json", "<<<JSON_BEGIN>>>{}", "<<<JSON_BEGIN>>>bad<<<JSON_END>>>", '{"bad":NaN}', '[{"nested":true}]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_json(text)


class DigestAdapter(FakeAdapter):
    def __init__(self, failures: int = 0, *, marked: bool = False) -> None:
        super().__init__()
        self.failures = failures
        self.marked = marked
        self.reads = 0

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model.startswith("reader"):
            self.reads += 1
            assert schema == ENGINE / "reoengine/_resources/schemas/review-digest.json"
            assert result.final_message_path is not None
            path = result.final_message_path
            if self.reads <= self.failures:
                path.write_text('{"checklist":[{"id":"R1","satisfied":true}]}', encoding="utf-8")
            elif self.marked:
                path.write_text("LEAKED_REASONING\n<<<JSON_BEGIN>>>\n" + path.read_text(encoding="utf-8")
                                + "\n<<<JSON_END>>>\n", encoding="utf-8")
        return result


class StatusTests(TemporaryTest):
    def test_single_run_exit_codes(self) -> None:
        cases = (("waiting", Status.AWAITING_HUMAN, 0), ("failed", Status.FAILED, 1),
                 ("stopped", Status.STOPPED, 1), ("limit", Status.FAILED, 3))
        for run_id, status, expected in cases:
            with self.subTest(run_id=run_id):
                directory = self.root / "runs" / run_id
                save_state(directory, RunState(run_id, status=status))
                if expected == 3:
                    atomic_write(directory / "error.json", {"error": "synthetic limit", "exit_code": 3})
                self.assertEqual(main(["status", run_id], root=self.root, out=io.StringIO()), expected)

    def test_list_with_failed_run_is_informational(self) -> None:
        save_state(self.root / "runs/failed", RunState("failed", status=Status.FAILED))
        out = io.StringIO()
        self.assertEqual(main(["status"], root=self.root, out=out), 0)
        self.assertIn("failed: failed", out.getvalue())


class CLITests(TemporaryTest):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repository(self.root)
        self.config = self.root / "config"
        (self.config / "profiles").mkdir(parents=True)
        (self.config / "projects").mkdir()
        roles = ''.join(f'[roles.{name}]\nengine="codex"\nmodel="{name}"\nfallback=["{name}-alt"]\n'
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

    def cli(self, args: list[str], **kwargs: object) -> int:
        # kwargs are only used for the two explicitly supported injectable values.
        limit = kwargs.get("limit")
        with patch("reoengine.cli.codex_rate_limits", side_effect=AssertionError("No real agent probes")):
            return main(args, root=self.root, adapters={Engine.CODEX: self.fake}, out=self.out, err=self.err,
                        rate_limits=(lambda: limit) if isinstance(limit, LimitRemaining) else None)

    def run_task(self, **kwargs: object) -> int:
        return self.cli(["run", "synthetic", "--task", str(self.root / "task.json"), "--plan", str(self.root / "plan.json")], **kwargs)

    def run_directory(self) -> Path:
        return next((self.root / "runs").iterdir())

    def test_run_gates_review_wait_and_status(self) -> None:
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        directory = self.run_directory()
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)
        self.assertIn("diff:", self.out.getvalue())
        self.assertIn("checks:", self.out.getvalue())
        calls = len(self.fake.calls)
        self.assertEqual(self.cli(["resume", directory.name]), 0)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(self.cli(["status"]), 0)
        self.assertEqual(self.cli(["status", directory.name]), 0)
        self.assertEqual(main(["--help"], out=self.out, err=self.err), 0)

    def test_gate_failure_stops_after_configured_fix_rounds(self) -> None:
        self.fake.mode = "bad"
        self.assertEqual(self.run_task(), 1)
        directory = self.run_directory()
        self.assertEqual(load_state(directory).status, Status.STOPPED)
        # The fake coder repeats the same diff, so the no-progress stop fires on the second round.
        self.assertEqual(sum(call.model == "coder" for call in self.fake.calls), 2)
        self.assertEqual(json.loads((directory / "result.json").read_text())["stop_reason"], "no_progress")
        self.assertNotIn("reviewer", [call.model for call in self.fake.calls])
        self.assertIn('"stage":"fix_loop"', (directory / "events.jsonl").read_text())

    def test_arguments_config_and_limit_codes(self) -> None:
        for args in ([], ["run"], ["bogus"], ["switch", "missing", "coder"],
                     ["switch", "coder", "unconfigured"], ["switch", "coder", "coder", "--engine", "agy"],
                     ["switch", "coder", "coder", "--effort", "nope"],
                     ["switch", "--reset", "coder", "reader"], ["status", "../outside"]):
            with self.subTest(args=args):
                self.assertEqual(self.cli(args), 2)
        self.assertEqual(self.run_task(limit=LimitRemaining(0, 50)), 3)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.cli(["resume", self.run_directory().name]), 3)

    def test_switch_precedence_reset_and_config_unchanged(self) -> None:
        original = (self.config / "roles.toml").read_bytes()
        atomic_write(self.root / "state/tuning.json", {"roles": {"coder": {"effort": "low"}}})
        self.assertEqual(self.cli(["switch", "coder", "coder-alt", "--effort", "medium"]), 0)
        effective = load_configuration(self.config, self.root / "state")
        self.assertEqual((effective.roles["coder"].model, effective.roles["coder"].effort), ("coder-alt", Effort.MEDIUM))
        self.assertEqual(self.cli(["switch", "--reset", "coder"]), 0)
        self.assertEqual(load_configuration(self.config, self.root / "state").roles["coder"].effort, Effort.LOW)
        self.assertEqual((self.config / "roles.toml").read_bytes(), original)
        self.assertIn('"event":"role_switched"', (self.root / "state/events.jsonl").read_text())

    def test_switch_applies_at_next_stage_and_freezes_current_review(self) -> None:
        invoke = Pipeline._guarded_invoke

        def switch_between_calls(pipeline: Pipeline, role_cfg: RoleConfig, prompt: Path, cwd: Path,
                                 schema: Path | None, *, role: str = "") -> AgentResult:
            result = invoke(pipeline, role_cfg, prompt, cwd, schema, role=role)
            # An engine/user switch happens outside the agent's snapshot pair.
            # Writing overrides inside invoke is now an outside_write violation.
            if role_cfg.model == "coder":
                self.assertEqual(self.cli(["switch", "reader", "reader-alt"]), 0)
            if role_cfg.model == "reader-alt":
                self.assertEqual(self.cli(["switch", "reviewer", "reviewer-alt"]), 0)
            return result

        with patch.object(Pipeline, "_guarded_invoke", switch_between_calls):
            self.assertEqual(self.run_task(), 0, self.err.getvalue())
            self.assertEqual([call.model for call in self.fake.calls], ["coder", "reader-alt", "reviewer"])
            self.assertIn('"event":"role_switched"', (self.run_directory() / "events.jsonl").read_text())
            self.assertEqual(self.run_task(), 0, self.err.getvalue())
            self.assertEqual(self.fake.calls[-1].model, "reviewer-alt")

    def test_real_stderr_quota_uses_cross_engine_fallback(self) -> None:
        text = (self.config / "roles.toml").read_text()
        text = text.replace('engine="codex"', 'engine="agy"')
        text = text.replace('[roles.coder]\nengine="agy"\nmodel="coder"\nfallback=["coder-alt"]',
                            '[roles.coder]\nengine="codex"\nmodel="quota"\nfallback_chain=[{engine="agy",model="coder"}]')
        (self.config / "roles.toml").write_text(text)
        command = (sys.executable, str(ENGINE / "tests/fakes/agent.py"), "codex", "quota")
        code = main(["run", "synthetic", "--task", str(self.root / "task.json"), "--plan", str(self.root / "plan.json")],
                    root=self.root, adapters={Engine.CODEX: CodexAdapter(command), Engine.AGY: self.fake}, out=self.out, err=self.err,
                    rate_limits=lambda: LimitRemaining(90, 90))
        self.assertEqual(code, 0, self.err.getvalue())
        events = (self.run_directory() / "events.jsonl").read_text()
        self.assertIn('"event":"fallback_used"', events)
        self.assertEqual(self.fake.calls[0].model, "coder")
        self.assertTrue(all(record.limit_before is None and record.limit_after is None
                            for record in UsageLog(self.root / "state").read_days() if record.engine == Engine.AGY))

    def test_quota_chain_exhaustion_is_exit_three(self) -> None:
        command = (sys.executable, str(ENGINE / "tests/fakes/agent.py"), "codex", "quota")
        code = main(["run", "synthetic", "--task", str(self.root / "task.json"), "--plan", str(self.root / "plan.json")],
                    root=self.root, adapters={Engine.CODEX: CodexAdapter(command)}, out=self.out, err=self.err)
        self.assertEqual(code, 3, self.err.getvalue())
        self.assertEqual(load_state(self.run_directory()).status, Status.FAILED)
        records = UsageLog(self.root / "state").read_days()
        self.assertEqual([record.model for record in records], ["coder", "coder-alt"])

    @staticmethod
    def _data_from_call(call: object) -> dict[str, object]:
        prompt = call.prompt
        envelope = json.JSONDecoder().raw_decode(prompt.split("DATA_JSON:\n", 1)[1])[0]
        return envelope["data"]

    def test_cli_generated_plan_and_research(self) -> None:
        self.assertEqual(self.cli(["run", "synthetic", "--task", str(self.root / "task.json"), "--research"]), 0,
                         self.err.getvalue())
        self.assertEqual([call.model for call in self.fake.calls], ["researcher", "architect", "coder", "reader", "reviewer"])
        research = {"summary": "synthetic", "sources": [], "findings": [], "unknowns": []}
        for call in self.fake.calls:
            if call.model in ("architect", "coder"):
                self.assertEqual(self._data_from_call(call)["research"], research)

    def test_cli_omits_research_as_null_when_not_requested(self) -> None:
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        for call in self.fake.calls:
            if call.model in ("architect", "coder"):
                self.assertIsNone(self._data_from_call(call).get("research"))

    def test_process_dies_during_implement_and_cli_resumes(self) -> None:
        process = subprocess.run((sys.executable, str(ENGINE / "tests/fakes/cli_crash.py"), str(self.root)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        self.assertEqual(process.returncode, 37, process.stderr.decode())
        directory = self.run_directory()
        self.assertEqual(load_state(directory).stage, Stage.IMPLEMENT)
        self.assertEqual(self.cli(["resume", directory.name]), 1)
        self.assertEqual(self.cli(["resume", directory.name, "--remove-stale"]), 0, self.err.getvalue())
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)
        self.assertFalse((directory / "run.lock").exists())

    def test_digest_schema_retry_success_and_private_metadata(self) -> None:
        self.fake = DigestAdapter(1, marked=True)
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        reader_calls = [call for call in self.fake.calls if call.model == "reader"]
        self.assertEqual(len(reader_calls), 2)
        self.assertIn("The previous response failed validation:", reader_calls[1].prompt)
        self.assertIn("review-digest schema validation failed", reader_calls[1].prompt)
        contract = (ENGINE / "reoengine/_resources/gemini-format.txt").read_text(encoding="utf-8")
        self.assertTrue(all(call.prompt.endswith(contract) for call in reader_calls))
        events = [object_value(value, "event") for value in read_lines(self.run_directory() / "events.jsonl")]
        attempts = [event for event in events if event["event"] == "response_validated"]
        self.assertEqual([(event["response_attempt"], event["valid"]) for event in attempts], [(1, False), (2, True)])
        records = UsageLog(self.root / "state").read_days(now=datetime.now(timezone.utc))
        self.assertEqual([record.attempt for record in records if record.role == "reader"], [1, 2])
        for path in [self.run_directory() / "events.jsonl", *list((self.root / "state").glob("usage-*.jsonl"))]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("LEAKED_REASONING", text)
            self.assertNotIn("checklist", text)

    def test_digest_two_failures_stop_before_reviewer(self) -> None:
        self.fake = DigestAdapter(2)
        self.assertEqual(self.run_task(), 1)
        self.assertEqual(self.fake.reads, 2)
        self.assertNotIn("reviewer", [call.model for call in self.fake.calls])
        self.assertFalse((self.run_directory() / "review/review-digest.json").exists())

    def test_resume_failed_review_restarts_reader_only(self) -> None:
        self.fake = DigestAdapter(2)
        self.assertEqual(self.run_task(), 1)
        directory = self.run_directory()
        self.assertEqual(load_state(directory).stage, Stage.REVIEW)
        before = {path.name: path.read_bytes() for path in (directory / "checkpoints").glob("*.json")}
        self.assertEqual([call.model for call in self.fake.calls], ["coder", "reader", "reader"])
        self.assertEqual(self.cli(["resume", directory.name]), 0, self.err.getvalue())
        self.assertEqual(load_state(directory).status, Status.AWAITING_HUMAN)
        self.assertEqual([call.model for call in self.fake.calls], ["coder", "reader", "reader", "reader", "reviewer"])
        for name, content in before.items():
            self.assertEqual((directory / "checkpoints" / name).read_bytes(), content)
        events = [object_value(value, "event") for value in read_lines(directory / "events.jsonl")]
        self.assertEqual([event["response_attempt"] for event in events if event["event"] == "response_validated"], [1, 2, 1])
        self.assertEqual(sum(event["event"] == "stage_start" and event["stage"] == "gates" for event in events), 1)
        self.assertFalse((directory / "error.json").exists())

    def test_environment_and_injected_directories_from_other_cwd(self) -> None:
        with patch.dict(os.environ, {"REOENGINE_CONFIG_DIR": str(self.config), "REOENGINE_STATE_DIR": str(self.root / "s"),
                                     "REOENGINE_RUNS_DIR": str(self.root / "r")}):
            self.assertEqual(main(["preflight", "--summary"], out=self.out, err=self.err), 0)
        self.assertIn("no changes; insufficient data", self.out.getvalue())
        self.assertFalse((self.root / "s/tuning.json").exists())
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.assertEqual(main(["--config-dir", str(self.config), "--state-dir", str(self.root / "s"),
                               "tune", "--summary"], out=self.out, err=self.err), 0)
        self.assertIn("no changes; insufficient data", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.assertEqual(main(["--config-dir", str(self.config), "--state-dir", str(self.root / "s"),
                               "preflight", "--summary"], out=self.out, err=self.err), 0)
        self.assertEqual(self.err.getvalue(), "warning: 'preflight' is deprecated, use 'tune'\n")

    def test_dirty_source_is_metadata_warning_and_never_copied(self) -> None:
        (self.repo / "unchanged.txt").write_text("PRIVATE_UNCOMMITTED_EDIT", encoding="utf-8")
        (self.repo / ".env.synthetic").touch()
        self.assertEqual(self.run_task(), 0, self.err.getvalue())
        directory = self.run_directory()
        self.assertEqual((directory / "worktree/unchanged.txt").read_text(), "original\n")
        self.assertFalse((directory / "worktree/.env.synthetic").exists())
        self.assertIn("changed files: 1", self.out.getvalue())
        self.assertIn("untracked files: 1", self.out.getvalue())
        self.assertNotIn("PRIVATE_UNCOMMITTED_EDIT", self.out.getvalue())
        self.assertNotIn(".env.synthetic", (directory / "events.jsonl").read_text())

    def test_secret_name_in_committed_base_is_refused_without_reading(self) -> None:
        from pipeline import git_command
        git = Git(self.repo)
        blob = git.command(("hash-object", "-w", "--stdin"), stdin=b"").decode().strip()
        git.command(("update-index", "--add", "--cacheinfo", f"100644,{blob},.env.synthetic"))
        git_command(self.repo, "commit", "-qm", "synthetic prohibited base name")
        self.assertEqual(self.run_task(), 1)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.run_directory() / "worktree").exists())


class TunerTests(TemporaryTest):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state"
        self.log = UsageLog(self.state)
        self.outcomes = TaskOutcomeLog(self.state)
        roles = {"coder": RoleConfig(Engine.CODEX, "gpt-worker"),
                 "reader": RoleConfig(Engine.CODEX, "gemini-flash"),
                 "reviewer": RoleConfig(Engine.CODEX, "gpt-6-astra"),
                 "architect": RoleConfig(Engine.AGY, "claude-opus"),
                 "designer": RoleConfig(Engine.AGY, "gemini-4-argon-high")}
        self.configuration = Configuration(roles, Limits(), {}, {})

    def seed(self, role: str = "coder", count: int = 10, *, effort: Effort = Effort.HIGH,
             tokens: int = 100, when: datetime | None = None, prefix: str = "baseline",
             limit: LimitRemaining | None = None, failed: int = 0) -> None:
        cfg = self.configuration.roles[role]
        start = when or NOW - timedelta(days=1)
        for index in range(count):
            record = UsageRecord(f"{prefix}-{role}-{index}", Stage.IMPLEMENT, role, cfg.engine, cfg.model,
                                 effort, TokenUsage(tokens, 0, 0, 0), 10, 0, "accepted",
                                 2 if index < failed else 1, limit, None, start + timedelta(minutes=index))
            if index < failed:
                self.log.append(replace(record, outcome="gate_failed", attempt=1,
                                        ts=record.ts - timedelta(seconds=1)))
            self.log.append(record)
            self.outcomes.append(TaskOutcome(record.run_id, 2 if index < failed else 1, record.ts))

    def tune(self, *, apply: bool = True, now: datetime = NOW) -> list[str]:
        return preflight(self.configuration, self.state, self.root / "runs", apply=apply, now=now)

    def tuning(self) -> dict[str, object]:
        return json.loads((self.state / "tuning.json").read_text(encoding="utf-8"))

    def test_empty_cold_start_and_default_dry_run(self) -> None:
        self.assertIn("no changes; insufficient data", self.tune(apply=False))
        self.assertFalse((self.state / "tuning.json").exists())
        self.seed(count=9)
        self.assertIn("no changes; insufficient data", self.tune())
        self.assertEqual(self.tuning(), {"roles": {}})

    def test_reduction_one_step_and_dry_run_does_not_write(self) -> None:
        self.seed()
        lines = self.tune(apply=False)
        self.assertTrue(any("Dry-run" in line for line in lines))
        self.assertFalse((self.state / "tuning.json").exists())
        self.tune()
        self.assertEqual(self.tuning()["roles"]["coder"]["effort"], "medium")
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)
        self.tune(now=NOW + timedelta(hours=1))
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)

    def test_gate_failure_increases_effort_and_metrics_include_retries(self) -> None:
        self.seed(failed=4)
        self.tune()
        self.assertEqual(self.tuning()["roles"]["coder"]["effort"], "xhigh")
        stats = metrics(self.log.read_days(now=NOW), self.outcomes.read_all())
        self.assertEqual((stats.accepted, stats.repeats, stats.gate_rate, stats.first_rate), (10, 4, 0.4, 0.6))
        self.assertEqual(stats.tokens_per_accepted, 140)

    def test_duplicate_attempts_do_not_satisfy_cold_start(self) -> None:
        self.seed(count=9)
        rows = self.log.read_days(now=NOW)
        self.log.append(replace(rows[-1], ts=NOW - timedelta(hours=1), attempt=2))
        self.tune()
        self.assertEqual(self.tuning(), {"roles": {}})

    def test_rollback_on_token_growth_and_freeze_for_fourteen_days(self) -> None:
        self.seed()
        self.tune()
        self.seed(count=5, effort=Effort.MEDIUM, tokens=121, when=NOW + timedelta(minutes=1), prefix="after")
        lines = self.tune(now=NOW + timedelta(hours=1))
        self.assertTrue(any("Rollback" in line for line in lines))
        self.assertNotIn("effort", self.tuning()["roles"]["coder"])
        log = [object_value(value, "log") for value in read_lines(self.state / "tuning-log.jsonl")]
        self.assertEqual(log[-1]["action"], "rollback")
        freeze_until = datetime.fromisoformat(log[-1]["freeze_until"])
        self.assertEqual(freeze_until, NOW + timedelta(hours=1, days=14))
        self.assertTrue(any("Frozen" in line for line in self.tune(now=NOW + timedelta(hours=2))))
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 2)
        self.seed(when=NOW + timedelta(days=15), prefix="thawed")
        self.tune(now=NOW + timedelta(days=15, hours=1))
        self.assertEqual(self.tuning()["roles"]["coder"]["effort"], "medium")

    def test_rollback_on_acceptance_decline_after_five_runs_not_four(self) -> None:
        self.seed()
        self.tune()
        self.seed(count=4, effort=Effort.MEDIUM, when=NOW + timedelta(minutes=1), prefix="after", failed=2)
        self.tune(now=NOW + timedelta(hours=1))
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)
        self.seed(count=1, effort=Effort.MEDIUM, when=NOW + timedelta(hours=2), prefix="fifth")
        self.tune(now=NOW + timedelta(hours=3))
        self.assertEqual(object_value(read_lines(self.state / "tuning-log.jsonl")[-1], "log")["action"], "rollback")

    def test_effort_observation_skips_manual_effort_changes(self) -> None:
        self.seed()
        self.tune()
        before = self.tuning()
        self.seed(count=5, effort=Effort.HIGH, tokens=1000, failed=5,
                  when=NOW + timedelta(minutes=1), prefix="manual")
        self.seed(count=5, effort=Effort.MEDIUM, when=NOW + timedelta(minutes=10), prefix="after")
        lines = self.tune(now=NOW + timedelta(hours=1))
        self.assertFalse(any("Rollback" in line or "Observing:" in line for line in lines))
        self.assertEqual(self.tuning(), before)
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)

    def test_effort_observation_waits_for_five_matching_runs(self) -> None:
        self.seed()
        self.tune()
        before = self.tuning()
        self.seed(count=5, effort=Effort.HIGH, tokens=1000, failed=5,
                  when=NOW + timedelta(minutes=1), prefix="manual")
        self.seed(count=4, effort=Effort.MEDIUM, tokens=121,
                  when=NOW + timedelta(minutes=10), prefix="after")
        lines = self.tune(now=NOW + timedelta(hours=1))
        self.assertIn("Observing: roles.coder.effort, 4/5 runs", lines)
        self.assertEqual(self.tuning(), before)
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)

    def test_slot_observation_uses_cell_effort_and_skips_manual_changes(self) -> None:
        self.configuration = replace(self.configuration, roles={**self.configuration.roles,
            "reader": replace(self.configuration.roles["reader"], effort=Effort.MEDIUM)})
        self.seed("reader", effort=Effort.MEDIUM, limit=LimitRemaining(19, 90))
        self.tune()
        before = self.tuning()
        self.seed("reader", count=5, effort=Effort.HIGH, tokens=1000, failed=5,
                  when=NOW + timedelta(minutes=1), prefix="manual")
        lines = self.tune(now=NOW + timedelta(hours=1))
        self.assertIn("Observing: limits.slots.readers, 0/5 runs", lines)
        self.assertEqual(self.tuning(), before)
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)
        self.seed("reader", count=4, effort=Effort.MEDIUM, tokens=121,
                  when=NOW + timedelta(hours=2), prefix="after")
        lines = self.tune(now=NOW + timedelta(hours=3))
        self.assertIn("Observing: limits.slots.readers, 4/5 runs", lines)
        self.assertEqual(self.tuning(), before)
        self.assertEqual(len(read_lines(self.state / "tuning-log.jsonl")), 1)
        self.seed("reader", count=1, effort=Effort.MEDIUM, tokens=121,
                  when=NOW + timedelta(hours=4), prefix="fifth")
        lines = self.tune(now=NOW + timedelta(hours=5))
        self.assertTrue(any("Rollback: limits.slots.readers" in line for line in lines))
        self.assertEqual(apply_runtime_tuning(self.configuration, self.state / "tuning.json").limits.slots["readers"], 4)
        history = read_lines(self.state / "tuning-log.jsonl")
        self.assertEqual(len(history), 2)
        self.assertEqual(object_value(history[-1], "log")["action"], "rollback")

    def test_deciding_roles_never_change_and_manual_override_wins(self) -> None:
        for role in ("reviewer", "architect", "designer", "coder"):
            self.seed(role)
        atomic_write(self.state / "overrides.json", {"roles": {"coder": {"effort": "low"}}})
        lines = self.tune()
        self.assertEqual(self.tuning(), {"roles": {}})
        self.assertEqual(sum("manual only" in line for line in lines), 3)

    def test_limit_pressure_one_slot_step_writer_unchanged_and_cold_start(self) -> None:
        self.seed("reader", count=9, limit=LimitRemaining(19, 90))
        self.tune()
        self.assertNotIn("limits", self.tuning())
        self.seed("reader", count=1, prefix="tenth", limit=LimitRemaining(19, 90))
        self.tune()
        runtime = apply_runtime_tuning(self.configuration, self.state / "tuning.json")
        self.assertEqual(runtime.limits.slots["readers"], 3)
        self.assertEqual(runtime.limits.slots["coder"], 1)
        self.tune(now=NOW + timedelta(hours=1))
        self.assertEqual(apply_runtime_tuning(self.configuration, self.state / "tuning.json").limits.slots["readers"], 3)

    def test_slot_and_skill_ceilings_and_forbidden_knobs(self) -> None:
        cfg = replace(self.configuration, profiles={"synthetic": Profile(skills=("a", "b"), max_skills=2)})
        for tuning in ({"roles": {}, "limits": {"slots": {"readers": 9}}},
                       {"roles": {}, "limits": {"slots": {"coder": 2}}},
                       {"roles": {}, "limits": {"sandbox": "anything"}},
                       {"roles": {}, "skills": {"synthetic": 3}}):
            atomic_write(self.state / "tuning.json", tuning)
            with self.subTest(tuning=tuning), self.assertRaises(ValueError):
                apply_runtime_tuning(cfg, self.state / "tuning.json")
        atomic_write(self.state / "tuning.json", {"roles": {}, "limits": {"slots": {"readers": 8}}, "skills": {"synthetic": 1}})
        runtime = apply_runtime_tuning(cfg, self.state / "tuning.json")
        self.assertEqual(runtime.limits.slots["readers"], 8)
        self.assertEqual(runtime.profiles["synthetic"].skills, ("a",))

    def test_only_fourteen_days_and_future_excluded(self) -> None:
        self.seed(when=NOW - timedelta(days=15), prefix="old")
        self.seed(when=NOW + timedelta(days=1), prefix="future")
        self.tune()
        self.assertEqual(self.tuning(), {"roles": {}})


class MaintenanceTests(TemporaryTest):
    def test_gc_expiration_and_preflight_scope(self) -> None:
        runs = self.root / "runs"
        expired = runs / "one/fail/old.json"
        live = runs / "one/fail/live.json"
        unrelated = runs / "one/review/old.json"
        malformed = runs / "one/fail/malformed.json"
        for path, when in ((expired, NOW - timedelta(seconds=1)), (live, NOW + timedelta(days=7)),
                           (unrelated, NOW - timedelta(days=10))):
            atomic_write(path, {"expires_at": when.isoformat(), "tail": "synthetic"})
        atomic_write(malformed, {"expires_at": "invalid"})
        self.assertEqual(collect_failures(runs, now=NOW), 1)
        self.assertFalse(expired.exists())
        self.assertTrue(all(path.exists() for path in (live, unrelated, malformed)))
        cfg = Configuration({}, Limits(), {}, {})
        preflight(cfg, self.root / "state", runs, now=NOW + timedelta(days=8))
        self.assertFalse(live.exists())
        self.assertTrue(unrelated.exists())

    def test_gc_never_follows_external_link(self) -> None:
        outside = self.root / "outside"
        atomic_write(outside / "old.json", {"expires_at": (NOW - timedelta(days=10)).isoformat()})
        run = self.root / "runs/one"
        run.mkdir(parents=True)
        link = run / "fail"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        self.assertEqual(collect_failures(self.root / "runs", now=NOW), 0)
        self.assertTrue((outside / "old.json").exists())

    def test_doctor_uses_only_version_flags_and_does_not_print_settings(self) -> None:
        settings = self.root / "home/.claude/settings.json"
        atomic_write(settings, {"permissions": {"allow": ["Bash(agy:*)", "Bash(codex exec:*)"]}, "private": "NOT_FOR_REPORT"})
        config = self.root / "config"
        config.mkdir()
        calls = []

        def probe(argv: tuple[str, ...]) -> ProcessResult:
            calls.append(argv)
            self.assertEqual(argv[1:], ("--version",))
            return ProcessResult(0, "synthetic 1.0", "", 0.01)

        out = io.StringIO()
        # The synthetic neutral folder is under this checkout, which has project
        # instructions. Isolate only that environment check in the healthy case.
        with patch("reoengine.doctor._validate_neutral_directory"):
            code = main(["doctor"], root=self.root, home=self.root / "home", neutral_directory=self.root / "neutral",
                        doctor_probe=probe, out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(calls), 4)
        self.assertNotIn("NOT_FOR_REPORT", out.getvalue())
        self.assertEqual(json.loads(settings.read_text())["private"], "NOT_FOR_REPORT")

    def test_doctor_missing_binary_config_and_unsafe_neutral(self) -> None:
        neutral = self.root / "neutral"
        (neutral / ".git").mkdir(parents=True)
        diagnostics = diagnose(self.root / "missing", self.root / "state", home=self.root / "home",
                               neutral_directory=neutral, python_version=(3, 10, 0),
                               probe=lambda argv: ProcessResult(1, "", "", 0.01))
        self.assertTrue(any(item.name == "git" and item.level == "error" for item in diagnostics))
        self.assertTrue(any(item.name == "python" and item.level == "error" for item in diagnostics))
        self.assertTrue(any(item.name == "config" and item.level == "error" for item in diagnostics))
        self.assertTrue(any(item.name == "codex" and item.level == "warning" for item in diagnostics))
        self.assertTrue(any(item.name == "neutral" and item.level == "error" for item in diagnostics))

    def test_factory_includes_fallback_engines_and_neutral_rejects_project(self) -> None:
        from reoengine.config import Fallback, Project
        roles = {"architect": RoleConfig(Engine.AGY, "claude-opus", fallback=("gpt-worker",),
                 fallback_chain=(Fallback(Engine.CLAUDE, "opus", True),))}
        config = Configuration(roles, Limits(), {}, {})
        fake = FakeAdapter()
        with self.assertRaisesRegex(ValueError, "Missing injected adapter"):
            make_adapters(config, supplied={Engine.AGY: fake})
        adapters = make_adapters(config, supplied={engine: fake for engine in Engine})
        self.assertEqual(set(adapters), set(Engine))
        root = make_repository(self.root)
        config = replace(config, projects={"synthetic": Project(root, "synthetic")})
        with self.assertRaisesRegex(ValueError, "outside every configured project"):
            make_adapters(config, neutral_directory=root / "neutral")
