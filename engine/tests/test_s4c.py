from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, git_command, make_inputs, make_pipeline, make_repository
from reoengine._data import read_lines
from reoengine.adapters.agy import AgyAdapter
from reoengine.adapters.base import AgentResult, ErrorKind
from reoengine.config import Engine, RoleConfig
from reoengine.events import Event, EventKind
from reoengine.fsm import OutsideWrite, Pipeline, RunInputs
from reoengine.gitops import Git
from reoengine.guard import Category, OutsideGuard, SnapshotError, SourceSnapshot, source_snapshot
from reoengine.state import Stage, Status, load_state


class WritingAdapter(FakeAdapter):
    def __init__(self, action: Callable[[], None], role: str = "coder", *, raises: bool = False,
                 quota: bool = False) -> None:
        super().__init__()
        self.action, self.role, self.raises, self.quota = action, role, raises, quota

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model == self.role:
            self.action()
            if self.raises:
                raise RuntimeError("synthetic failure after writing")
            if self.quota:
                return replace(result, exit_code=29, error=ErrorKind.QUOTA)
        return result


class SyntheticTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reoengine-s4c-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = make_repository(self.directory)
        self.config = self.directory / "config"
        self.config.mkdir()
        (self.config / "nested").mkdir()
        (self.config / "nested/roles.toml").write_text("synthetic", encoding="utf-8")

    def pipeline(self, adapter: FakeAdapter | None = None) -> Pipeline:
        return make_pipeline(self.directory, self.repo, adapter or FakeAdapter(), config_directory=self.config)

    def events(self, name: str) -> list[dict[str, object]]:
        values = read_lines(self.directory / "run/events.jsonl")
        return [value for value in values if isinstance(value, dict) and value.get("event") == name]

    def assert_source_snapshot_failure(self, pipeline: Pipeline, fake: FakeAdapter, calls: int) -> None:
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(pipeline.state.stage, Stage.IMPLEMENT)
        self.assertEqual(pipeline.exit_code, 1)
        self.assertEqual(load_state(pipeline.directory).status, Status.FAILED)
        error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
        self.assertEqual(error, {"stage": "implement", "error": "outside_write:source_repo", "exit_code": 1,
                                 "transient": False})
        events = [v for v in read_lines(pipeline.events.path) if isinstance(v, dict)]
        detections = [e for e in events if e.get("event") == "outside_write_detected"]
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0]["category"], "source_repo")
        self.assertEqual(detections[0]["changed_file_count"], 1)
        self.assertEqual(detections[0]["stage"], "implement")
        for private in (str(self.repo), "unchanged.txt", "node_modules", "overflow", "synthetic damage"):
            self.assertNotIn(private, json.dumps(error) + json.dumps(detections))
        self.assertEqual(len(fake.calls), calls)
        self.assertNotIn("implement:1", pipeline.state.output_hashes)
        self.assertFalse((pipeline.directory / "checkpoints/implement-1.json").exists())
        self.assertFalse(any(e.get("event") == "agent_end" for e in events))
        result = json.loads((pipeline.directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["gates"], {"passed": False, "checks": [], "isolation": "env-only"})
        self.assertIsNone(result["review"])


class GuardTests(SyntheticTest):
    def test_writes_to_each_category_fail_and_preserve_evidence(self) -> None:
        targets = {"source_repo": self.repo / "synthetic-untracked.txt",
                   "engine_state": self.directory / "run/state.json",
                   "config": self.config / "nested/roles.toml",
                   "state_dir": self.directory / "state/overrides.json"}
        for category, target in targets.items():
            with self.subTest(category=category), tempfile.TemporaryDirectory(prefix="reoengine-case-") as name:
                case = Path(name)
                # Each pipeline has its own run/state; source and config remain synthetic.
                if category == "engine_state":
                    target = case / "run/state.json"
                elif category == "state_dir":
                    target = case / "state/overrides.json"
                fake = WritingAdapter(lambda: target.write_text("synthetic damage", encoding="utf-8"))
                pipeline = make_pipeline(case, self.repo, fake, config_directory=self.config)
                state = pipeline.start(make_inputs(tdd=False))
                self.assertEqual(state.status, Status.FAILED)
                self.assertEqual(pipeline.exit_code, 1)
                error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
                self.assertEqual(error["error"], f"outside_write:{category}")
                self.assertEqual(target.read_text(encoding="utf-8"), "synthetic damage")
                events = [v for v in read_lines(pipeline.events.path)
                          if isinstance(v, dict) and v.get("event") == "outside_write_detected"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["category"], category)
                self.assertEqual(events[0]["changed_file_count"], 1)
                self.assertNotIn(str(target), json.dumps(events))
                self.assertNotIn("synthetic damage", json.dumps(events))
                self.assertEqual([call.model for call in fake.calls], ["coder"])

    def test_normal_run_accepts_engine_events_and_dirty_source(self) -> None:
        (self.repo / "unchanged.txt").write_text("dirty before", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("dirty before", encoding="utf-8")
        pipeline = self.pipeline()
        state = pipeline.start(make_inputs(tdd=False))
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        self.assertTrue(self.events("source_dirty"))
        self.assertFalse(self.events("outside_write_detected"))

    def test_engine_appends_events_during_agent_call(self) -> None:
        fake = WritingAdapter(lambda: pipeline.events.append(Event("run", EventKind.RATE_LIMITS_UNAVAILABLE)))
        pipeline = self.pipeline(fake)
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.FAILED)
        self.assertTrue(self.events("outside_write_detected"))

    def test_git_unavailable_fails_before_or_after_call(self) -> None:
        real_snapshot = source_snapshot
        for failed_snapshot in (1, 2):
            with self.subTest(failed_snapshot=failed_snapshot), tempfile.TemporaryDirectory() as name:
                fake = FakeAdapter()
                pipeline = make_pipeline(Path(name), self.repo, fake, config_directory=self.config)
                calls = 0

                def unavailable(git: Git) -> SourceSnapshot | None:
                    nonlocal calls
                    calls += 1
                    return None if calls == failed_snapshot else real_snapshot(git)

                with patch("reoengine.guard.source_snapshot", side_effect=unavailable):
                    pipeline.start(make_inputs(tdd=False))
                self.assert_source_snapshot_failure(pipeline, fake, failed_snapshot - 1)
                self.assertEqual(calls, failed_snapshot)
        with patch.object(pipeline.source, "command", side_effect=ValueError("synthetic git failure")):
            self.assertIsNone(source_snapshot(pipeline.source))

    def test_unavailable_snapshot_is_stage_error_for_direct_execution(self) -> None:
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        pipeline._set_inputs(make_inputs(tdd=False))
        for stage in (Stage.PREFLIGHT, Stage.BRIEF, Stage.PLAN):
            pipeline.execute_stage(stage)
        with patch("reoengine.guard.source_snapshot", return_value=None):
            with self.assertRaisesRegex(SnapshotError, "^outside_snapshot_unavailable:source_repo$"):
                pipeline.guard.snapshot()
            with self.assertRaisesRegex(OutsideWrite, "^outside_write:source_repo$") as error:
                pipeline.execute_stage(Stage.IMPLEMENT)
        self.assertFalse(error.exception.preserve_state)
        self.assertEqual(fake.calls, [])

    def test_source_metadata_only_without_path_iterdir(self) -> None:
        secret = self.repo / ".env.synthetic"
        secret.touch()
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/synthetic.txt").touch()
        pipeline = self.pipeline()
        with patch.object(Path, "open", side_effect=AssertionError("source content read")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("source walk")):
            before = source_snapshot(pipeline.source)
            self.assertIsNotNone(before)
            info = secret.stat()
            os.utime(secret, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            self.assertNotEqual(before, source_snapshot(pipeline.source))

    def test_all_roles_and_exceptions_are_guarded(self) -> None:
        for role in ("coder", "reader", "reviewer", "researcher", "architect"):
            with self.subTest(role=role), tempfile.TemporaryDirectory(prefix="reoengine-role-") as name:
                target = self.config / "nested/roles.toml"
                fake = WritingAdapter(lambda: target.write_text(role, encoding="utf-8"), role, raises=True)
                pipeline = make_pipeline(Path(name), self.repo, fake, config_directory=self.config)
                inputs = make_inputs(tdd=False)
                if role == "researcher":
                    inputs = replace(inputs, research=True)
                if role == "architect":
                    inputs = RunInputs(inputs.task)
                self.assertEqual(pipeline.start(inputs).status, Status.FAILED)
                error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
                self.assertEqual(error["error"], "outside_write:config")

    def test_mutation_prevents_quota_fallback(self) -> None:
        target = self.config / "nested/roles.toml"
        fake = WritingAdapter(lambda: target.write_text("damage", encoding="utf-8"), quota=True)
        pipeline = self.pipeline(fake)
        roles = dict(pipeline.config.roles)
        roles["coder"] = replace(roles["coder"], fallback=("coder-alt",))
        pipeline.role_provider = lambda: roles
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.FAILED)
        self.assertEqual([call.model for call in fake.calls], ["coder"])

    def test_events_prefix_rewrite_truncate_and_delete_are_rejected(self) -> None:
        for mode in ("rewrite", "truncate", "delete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="reoengine-events-") as name:
                run = Path(name)
                events = run / "events.jsonl"
                original = b'{"synthetic":"original"}\n'
                events.write_bytes(original)
                guard = OutsideGuard(run, self.config, self.directory / "state", self.pipeline().source)
                before = guard.snapshot()
                if mode == "delete":
                    events.unlink()
                elif mode == "truncate":
                    events.write_bytes(b"")
                else:
                    events.write_bytes(b'{"synthetic":"modified"}\nmore bytes\n')
                violations = guard.compare(before, guard.snapshot())
                self.assertEqual([(v.category, v.changed_path_count) for v in violations], [("engine_state", 1)])

    def test_snapshot_inclusions_exclusions_and_path_counts(self) -> None:
        pipeline = self.pipeline()
        guard = pipeline.guard
        before = guard.snapshot()
        for name in ("worktree/a.txt", "prompts/a.txt", "fail/a.json", "usage/a.json", "feedback.json"):
            path = pipeline.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("excluded", encoding="utf-8")
        pipeline.state_directory.mkdir(exist_ok=True)
        (pipeline.state_directory / "usage-synthetic.jsonl").write_text("excluded", encoding="utf-8")
        self.assertEqual(guard.compare(before, guard.snapshot()), ())
        targets: tuple[tuple[Category, Path], ...] = (
            ("engine_state", pipeline.directory / "checkpoints/nested/stage.json"),
            ("engine_state", pipeline.directory / "review/diff.patch"),
            ("state_dir", pipeline.state_directory / "tuning.json"),
            ("state_dir", pipeline.state_directory / "tuning-log.jsonl"),
            ("config", self.config / "nested/new.toml"),
        )
        for category, path in targets:
            with self.subTest(path=path.name):
                before = guard.snapshot()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("included", encoding="utf-8")
                violations = guard.compare(before, guard.snapshot())
                self.assertEqual([(v.category, v.changed_path_count) for v in violations], [(category, 1)])
        before = guard.snapshot()
        for name in ("one.toml", "two.toml"):
            (self.config / name).touch()
        self.assertEqual(guard.compare(before, guard.snapshot())[0].changed_path_count, 2)

    def test_source_rename_and_head_changes_are_detected(self) -> None:
        guard = self.pipeline().guard
        before = guard.snapshot()
        git_command(self.repo, "mv", "unchanged.txt", "renamed file.txt")
        after = guard.snapshot()
        assert after.source is not None
        self.assertEqual({name for name in after.source.paths if not name.startswith("gitdir:")},
                         {"unchanged.txt", "renamed file.txt"})
        self.assertEqual([(v.category, v.changed_path_count) for v in guard.compare(before, after)],
                         [("source_repo", 2)])
        git_command(self.repo, "commit", "-qm", "synthetic rename")
        before = guard.snapshot()
        git_command(self.repo, "commit", "--allow-empty", "-qm", "synthetic head change")
        self.assertEqual([(v.category, v.changed_path_count) for v in guard.compare(before, guard.snapshot())],
                         [("source_repo", 1)])

    def test_corrupted_event_tail_is_preserved_on_failure(self) -> None:
        damage = b"synthetic truncated event"
        fake = WritingAdapter(lambda: pipeline.events.path.write_bytes(damage))
        pipeline = self.pipeline(fake)
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.FAILED)
        self.assertTrue(pipeline.events.path.read_bytes().startswith(damage + b"\n"))
        self.assertIn(b'"event":"outside_write_detected"', pipeline.events.path.read_bytes())
        self.assertFalse(pipeline.events.path.with_suffix(".jsonl.corrupt").exists())

    def test_event_growth_while_snapshot_is_taken_is_detected(self) -> None:
        pipeline = self.pipeline()
        pipeline.directory.mkdir(parents=True, exist_ok=True)
        pipeline.events.path.write_bytes(b'{"synthetic":"before"}\n')
        lstat = Path.lstat
        appended = False

        def concurrent_append(path: Path) -> os.stat_result:
            nonlocal appended
            info = lstat(path)
            if path == pipeline.events.path and not appended:
                appended = True
                with path.open("ab") as stream:
                    stream.write(b'{"synthetic":"during snapshot"}\n')
            return info

        with patch.object(Path, "lstat", concurrent_append):
            before = pipeline.guard.snapshot()
        self.assertTrue(appended)
        self.assertTrue(pipeline.guard.compare(before, pipeline.guard.snapshot()))


class DigestSchemaTests(SyntheticTest):
    def run_reader(self, mode: str) -> tuple[Pipeline, FakeAdapter, list[dict[str, object]]]:
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        trace = pipeline.directory / "prompts/reader-trace.jsonl"
        roles = dict(pipeline.config.roles)
        roles["reader"] = replace(roles["reader"], engine=Engine.AGY)
        pipeline.role_provider = lambda: roles
        pipeline.adapters = {Engine.CODEX: fake, Engine.AGY: AgyAdapter(
            (sys.executable, str(ENGINE / "tests/fakes/agy_digest.py"), mode, str(trace)),
            capabilities=("--sandbox", "--mode"))}
        pipeline.start(make_inputs(tdd=False))
        calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        return pipeline, fake, calls

    def test_structured_digest_and_reader_command_schema(self) -> None:
        pipeline, fake, calls = self.run_reader("good")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--json-schema") + 1], str(ENGINE / "reoengine/_resources/schemas/review-digest.json"))
        self.assertEqual([call.model for call in fake.calls], ["coder", "reviewer"])
        self.assertTrue((pipeline.directory / "review/review-digest.json").is_file())

    def test_adapter_checks_schema_failure_is_revalidated_after_substitution(self) -> None:
        pipeline, fake, calls = self.run_reader("invalid_checks")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 1)
        digest = json.loads((pipeline.directory / "review/review-digest.json").read_text(encoding="utf-8"))
        self.assertEqual(digest["checks"], pipeline._load(Stage.GATES)["checks"])
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [True])
        self.assertEqual([call.model for call in fake.calls], ["coder", "reviewer"])

    def test_schema_valid_bad_quote_gets_exactly_one_retry(self) -> None:
        pipeline, fake, calls = self.run_reader("retry_quote")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 2)
        self.assertIn("diff section", calls[1]["prompt"])
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, True])
        self.assertEqual([call.model for call in fake.calls].count("reviewer"), 1)

    def test_two_bad_quotes_fail_without_reviewer(self) -> None:
        pipeline, fake, calls = self.run_reader("bad_quote")
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("reviewer", [call.model for call in fake.calls])
        self.assertFalse((pipeline.directory / "review/review-digest.json").exists())

    def test_two_adapter_schema_failures_keep_raw_response_tails(self) -> None:
        pipeline, fake, calls = self.run_reader("bad_schema")
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        tails = list((pipeline.directory / "fail").glob("reader-*.json"))
        self.assertEqual(len(tails), 2)
        for path in tails:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn('"suspicious"', data["tail"])
            self.assertLessEqual(len(data["tail"].encode("utf-8")), 4096)

    def test_adapter_schema_rejection_also_retries_once(self) -> None:
        pipeline, _, calls = self.run_reader("retry_schema")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 2)
        self.assertIn("review-digest schema", calls[1]["prompt"])

    def assert_invalid_checklist(self, mode: str) -> None:
        pipeline, fake, calls = self.run_reader(mode)
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
        self.assertIn("invalid_response:reader:two_validation_failures", error["error"])
        self.assertIn("Digest does not cover task/check results", calls[1]["prompt"])
        self.assertFalse((pipeline.directory / "review/review-digest.json").exists())

    def test_duplicate_checklist_id_is_rejected(self) -> None:
        self.assert_invalid_checklist("duplicate_id")

    def test_conflicting_duplicate_checklist_id_is_rejected(self) -> None:
        self.assert_invalid_checklist("conflicting_id")

    def test_missing_checklist_id_is_rejected(self) -> None:
        self.assert_invalid_checklist("missing_id")

    def test_extra_checklist_id_is_rejected(self) -> None:
        self.assert_invalid_checklist("extra_id")

    def test_duplicate_checklist_can_be_corrected_on_retry(self) -> None:
        pipeline, fake, calls = self.run_reader("retry_duplicate")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, True])
        self.assertEqual([call.model for call in fake.calls].count("reviewer"), 1)

    def test_line_outside_hunk_can_be_corrected_on_retry(self) -> None:
        pipeline, fake, calls = self.run_reader("retry_line")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 2)
        self.assertIn("inside a hunk", calls[1]["prompt"])
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, True])
        self.assertEqual([call.model for call in fake.calls].count("reviewer"), 1)

    def test_two_out_of_hunk_lines_fail_without_reviewer(self) -> None:
        pipeline, fake, calls = self.run_reader("bad_line")
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, False])
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        self.assertFalse((pipeline.directory / "review/review-digest.json").exists())

    def test_mismatched_quote_line_inside_hunk_can_be_corrected_on_retry(self) -> None:
        pipeline, fake, calls = self.run_reader("retry_mismatched_line")
        self.assertEqual(pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertEqual(len(calls), 2)
        self.assertIn("first excerpt line", calls[1]["prompt"])
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, True])
        self.assertEqual([call.model for call in fake.calls].count("reviewer"), 1)

    def test_two_mismatched_quote_lines_fail_without_reviewer(self) -> None:
        pipeline, fake, calls = self.run_reader("bad_mismatched_line")
        self.assertEqual(pipeline.state.status, Status.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["valid"] for e in self.events("response_validated")], [False, False])
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        self.assertFalse((pipeline.directory / "review/review-digest.json").exists())


class IgnoredSourceTests(SyntheticTest):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".gitignore").write_text(".env*\nnode_modules/\n", encoding="utf-8")
        self.ignored = self.repo / ".env.synthetic sample"

    def entry_limit(self, ignored_entries: int) -> int:
        snapshot = source_snapshot(self.pipeline().source)
        assert snapshot is not None
        return sum(name.startswith("gitdir:") for name in snapshot.paths) + ignored_entries

    def assert_ignored_change(self, action: Callable[[], None], *, exact_count: int | None = 1) -> None:
        fake = WritingAdapter(action)
        pipeline = self.pipeline(fake)
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.FAILED)
        self.assertEqual(pipeline.exit_code, 1)
        error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
        self.assertEqual(error["error"], "outside_write:source_repo")
        detections = self.events("outside_write_detected")
        self.assertEqual([e["category"] for e in detections], ["source_repo"])
        if exact_count is not None:
            self.assertEqual(detections[0]["changed_file_count"], exact_count)
        else:
            self.assertGreater(int(str(detections[0]["changed_file_count"])), 0)
        self.assertNotIn(self.ignored.name, json.dumps(error) + json.dumps(detections))
        self.assertNotIn("node_modules", json.dumps(error) + json.dumps(detections))
        self.assertEqual([call.model for call in fake.calls], ["coder"])

    def test_existing_ignored_file_metadata_change_is_detected(self) -> None:
        self.ignored.touch()
        info = self.ignored.stat()
        self.assert_ignored_change(lambda: os.utime(self.ignored, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000)))

    def test_new_ignored_file_is_detected(self) -> None:
        self.assert_ignored_change(self.ignored.touch)

    def test_deleted_ignored_file_is_detected(self) -> None:
        self.ignored.touch()
        self.assert_ignored_change(self.ignored.unlink)

    def test_unchanged_ignored_file_has_no_false_positive(self) -> None:
        self.ignored.touch()
        pipeline = self.pipeline()
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.AWAITING_HUMAN)
        self.assertFalse(self.events("outside_write_detected"))

    def nested_file(self) -> Path:
        nested = self.repo / "node_modules/deep/deeper/fixture.txt"
        nested.parent.mkdir(parents=True)
        nested.touch()
        return nested

    def test_ignored_paths_are_metadata_only_and_directory_contents_are_guarded(self) -> None:
        self.ignored.touch()
        nested = self.nested_file()
        guard = self.pipeline().guard
        with patch.object(Path, "open", side_effect=AssertionError("source content read")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("source walk")):
            before = source_snapshot(guard.source)
            assert before is not None
            self.assertTrue(before.paths[self.ignored.name].startswith(b"!!".hex() + ":"))
            self.assertIn("node_modules/", before.paths)
            self.assertIn("node_modules/deep", before.paths)
            self.assertIn("node_modules/deep/deeper", before.paths)
            self.assertIn("node_modules/deep/deeper/fixture.txt", before.paths)
            info = nested.stat()
            os.utime(nested, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            after = source_snapshot(guard.source)
            self.assertIsNotNone(after)
            self.assertNotEqual(before, after)

    def test_nested_ignored_file_overwrite_is_detected(self) -> None:
        nested = self.nested_file()
        self.assert_ignored_change(lambda: nested.write_text("synthetic replacement", encoding="utf-8"))

    def test_nested_ignored_file_creation_is_detected(self) -> None:
        nested = self.nested_file()
        self.assert_ignored_change((nested.parent / "new.txt").touch, exact_count=None)

    def test_nested_ignored_file_deletion_is_detected(self) -> None:
        nested = self.nested_file()
        self.assert_ignored_change(nested.unlink, exact_count=None)

    def test_unchanged_ignored_directory_has_no_false_positive(self) -> None:
        self.nested_file()
        pipeline = self.pipeline()
        self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.AWAITING_HUMAN)
        self.assertFalse(self.events("outside_write_detected"))

    def test_ignored_entry_limit_is_global_and_does_not_truncate(self) -> None:
        self.nested_file()
        self.ignored.touch()
        source = self.pipeline().source
        # Git entries share the budget with four tree entries and another !! root.
        limit = self.entry_limit(5)
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", limit):
            self.assertIsNotNone(source_snapshot(source))
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", limit - 1):
            self.assertIsNone(source_snapshot(source))

    def test_ignored_entry_limit_before_call_prevents_agent_launch(self) -> None:
        self.nested_file()
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", self.entry_limit(3)):
            pipeline.start(make_inputs(tdd=False))
        self.assert_source_snapshot_failure(pipeline, fake, 0)

    def test_ignored_entry_limit_after_call_rejects_tracked_source_write(self) -> None:
        nested = self.nested_file()
        tracked = self.repo / "unchanged.txt"

        def evade_snapshot() -> None:
            tracked.write_text("synthetic damage", encoding="utf-8")
            for index in range(2):
                (nested.parent / f"overflow-{index}.txt").touch()

        fake = WritingAdapter(evade_snapshot)
        pipeline = self.pipeline(fake)
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", self.entry_limit(4)):
            self.assertIsNotNone(source_snapshot(pipeline.source))
            pipeline.start(make_inputs(tdd=False))
            self.assertIsNone(source_snapshot(pipeline.source))
        self.assert_source_snapshot_failure(pipeline, fake, 1)
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        # Evidence remains on disk; verify source metadata/status without opening it.
        self.assertEqual(tracked.stat().st_size, len("synthetic damage"))
        self.assertIn(b" M unchanged.txt\0", git_command(self.repo, "status", "--porcelain=v1", "-z"))
        self.assertTrue((nested.parent / "overflow-1.txt").is_file())

    def test_ignored_entry_limit_boundary_allows_normal_run(self) -> None:
        self.nested_file()
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", self.entry_limit(4)):
            self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.AWAITING_HUMAN)
        self.assertEqual([call.model for call in fake.calls], ["coder", "reader", "reviewer"])
        self.assertFalse(self.events("outside_write_detected"))

    def test_ignored_walk_is_sorted_and_deterministic(self) -> None:
        nested = self.nested_file()
        for name in ("z.txt", "a.txt", "m.txt"):
            (nested.parent / name).touch()
        snapshot = source_snapshot(self.pipeline().source)
        assert snapshot is not None
        names = [name for name in snapshot.paths if name.startswith("node_modules/")]
        self.assertEqual(names, sorted(names))
        self.assertEqual(snapshot, source_snapshot(self.pipeline().source))

    def test_ignored_walk_errors_make_snapshot_unavailable(self) -> None:
        nested = self.nested_file()
        source = self.pipeline().source
        lstat, scandir = Path.lstat, os.scandir
        for target in (nested, nested.parent):
            for error in (PermissionError, OSError):
                def fail_stat(path: Path) -> os.stat_result:
                    if path == target:
                        raise error("synthetic inaccessible entry")
                    return lstat(path)

                with self.subTest(target=target.name, error=error), patch.object(Path, "lstat", fail_stat):
                    self.assertIsNone(source_snapshot(source))
        for error in (PermissionError, OSError):
            with self.subTest(error=error), patch("reoengine.guard.os.scandir", side_effect=error("synthetic walk error")):
                self.assertIsNone(source_snapshot(source))
        # A failure after scandir starts yielding must also invalidate the whole snapshot.
        def incomplete_scan(path: Path) -> object:
            if path == nested.parent:
                class BrokenScan:
                    def __enter__(self) -> BrokenScan:
                        return self

                    def __exit__(self, *args: object) -> None:
                        pass

                    def __iter__(self) -> object:
                        with scandir(path) as entries:
                            yield next(entries)
                            raise OSError("synthetic partial enumeration")
                return BrokenScan()
            return scandir(path)

        with patch("reoengine.guard.os.scandir", side_effect=incomplete_scan):
            self.assertIsNone(source_snapshot(source))

    def test_disappearance_during_lstat_or_scandir_counts_as_change(self) -> None:
        nested = self.nested_file()
        guard = self.pipeline().guard
        before = guard.snapshot()
        lstat, scandir = Path.lstat, os.scandir

        def missing_file(path: Path) -> os.stat_result:
            if path == nested:
                raise FileNotFoundError("synthetic disappearance")
            return lstat(path)

        def missing_directory(path: Path) -> object:
            if path == nested.parent:
                raise FileNotFoundError("synthetic disappearance")
            return scandir(path)

        for target, replacement in (("lstat", missing_file), ("scandir", missing_directory)):
            target_module = Path if target == "lstat" else os
            with self.subTest(target=target), patch.object(target_module, target, replacement):
                after = guard.snapshot()
            self.assertIsNotNone(after.source)
            self.assertEqual([v.category for v in guard.compare(before, after)], ["source_repo"])

    def assert_link_not_followed(self, link: Path, target: Path) -> None:
        source = self.pipeline().source
        scandir = os.scandir

        def no_link_scan(path: Path) -> object:
            self.assertNotEqual(path, link)
            self.assertNotEqual(path, target)
            return scandir(path)

        with patch("reoengine.guard.os.scandir", side_effect=no_link_scan), \
                patch.object(Path, "open", side_effect=AssertionError("source content read")):
            before = source_snapshot(source)
            assert before is not None
            self.assertIn(link.relative_to(self.repo).as_posix(), before.paths)
            self.assertFalse(any(name.endswith("/external.txt") for name in before.paths))
        (target / "external.txt").write_text("synthetic changed target", encoding="utf-8")
        with patch("reoengine.guard.os.scandir", side_effect=no_link_scan):
            self.assertEqual(before, source_snapshot(source))

    def test_symlink_inside_ignored_directory_is_not_followed(self) -> None:
        nested = self.nested_file()
        target = self.directory / "external"
        target.mkdir()
        (target / "external.txt").touch()
        link = nested.parent / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable on this platform")
        self.assert_link_not_followed(link, target)

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junction_inside_ignored_directory_is_not_followed(self) -> None:
        import _winapi

        nested = self.nested_file()
        target = self.directory / "external"
        target.mkdir()
        (target / "external.txt").touch()
        link = nested.parent / "junction"
        _winapi.CreateJunction(str(target), str(link))
        self.assert_link_not_followed(link, target)


if __name__ == "__main__":
    unittest.main()
