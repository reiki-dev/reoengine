"""Tests for rate limit isolation across engines and subscription usage summary."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_repository
from reoengine._data import loads
from reoengine.config import Configuration, Effort, Engine, Limits, RoleConfig, apply_runtime_tuning
from reoengine.events import LimitRemaining
from reoengine.fsm import Status
from reoengine.gc import usage_size_warnings
from reoengine.preflight import metrics, preflight
from reoengine.state import Stage
from reoengine.usage import TaskOutcome, TaskOutcomeLog, TokenUsage, UsageLog, UsageRecord

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


class LimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-lim-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.runs = self.root / "runs"
        self.log = UsageLog(self.state)
        self.outcomes = TaskOutcomeLog(self.state)

    def test_usage_size_warnings_only_when_file_exceeds_limit(self) -> None:
        self.state.mkdir(parents=True)
        limit = 1024 * 1024
        usage = self.state / "usage-2026-11.jsonl"
        oversized_usage = self.state / "usage-2026-10.jsonl"
        outcomes = self.state / "task-outcomes.jsonl"
        with usage.open("wb") as stream:
            stream.truncate(limit)
        with oversized_usage.open("wb") as stream:
            stream.truncate(limit + limit // 2)
        with outcomes.open("wb") as stream:
            stream.truncate(limit + limit // 2)
        (self.state / "usage-other.jsonl").write_bytes(b"ignored")

        self.assertEqual(usage_size_warnings(self.state, limit_bytes=limit),
                         ["usage-2026-10.jsonl: 1.500000 MiB exceeds 1 MiB",
                          "task-outcomes.jsonl: 1.500000 MiB exceeds 1 MiB"])

    def _seed(self, role_cfg: RoleConfig, role: str = "reader", count: int = 10,
              limit_after: LimitRemaining | None = None) -> None:
        for index in range(count):
            run_id = f"seed-{role}-{index}"
            ts = NOW - timedelta(days=1) + timedelta(minutes=index)
            record = UsageRecord(
                run_id, Stage.IMPLEMENT, role, role_cfg.engine, role_cfg.model,
                role_cfg.effort, TokenUsage(100, 0, 10, 0), 10, 0, "accepted",
                1, None, limit_after, ts,
            )
            self.log.append(record)
            self.outcomes.append(TaskOutcome(run_id, 1, ts))

    def test_reader_on_agy_with_high_codex_limit_does_not_reduce_slots(self) -> None:
        reader_cfg = RoleConfig(Engine.AGY, "gemini-flash")
        roles = {
            "coder": RoleConfig(Engine.CODEX, "gpt-worker"),
            "reader": reader_cfg,
            "reviewer": RoleConfig(Engine.CODEX, "gpt-6-astra"),
        }
        configuration = Configuration(roles, Limits(), {}, {})
        self._seed(reader_cfg, "reader", count=10,
                   limit_after=LimitRemaining(five_hour_percent=5.0, week_percent=5.0))
        lines = preflight(configuration, self.state, self.runs, apply=False, now=NOW)
        self.assertFalse(any("limits.slots.readers" in line for line in lines))
        lines_apply = preflight(configuration, self.state, self.runs, apply=True, now=NOW)
        self.assertFalse(any("limits.slots.readers" in line for line in lines_apply))
        runtime = apply_runtime_tuning(configuration, self.state / "tuning.json")
        self.assertEqual(runtime.limits.slots["readers"], 4)

    def test_reader_on_codex_with_high_limit_reduces_slots(self) -> None:
        reader_cfg = RoleConfig(Engine.CODEX, "gpt-reader")
        roles = {
            "coder": RoleConfig(Engine.CODEX, "gpt-worker"),
            "reader": reader_cfg,
            "reviewer": RoleConfig(Engine.CODEX, "gpt-6-astra"),
        }
        configuration = Configuration(roles, Limits(), {}, {})
        self._seed(reader_cfg, "reader", count=10,
                   limit_after=LimitRemaining(five_hour_percent=5.0, week_percent=5.0))
        lines = preflight(configuration, self.state, self.runs, apply=True, now=NOW)
        self.assertTrue(any("limits.slots.readers -> 3" in line for line in lines))
        runtime = apply_runtime_tuning(configuration, self.state / "tuning.json")
        self.assertEqual(runtime.limits.slots["readers"], 3)

    def test_used_5h_and_week_percent_calculated_separately_and_max(self) -> None:
        rec_5h_dominant = UsageRecord(
            "run-1", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_before=LimitRemaining(five_hour_percent=25.0, week_percent=70.0), ts=NOW,
        )
        stats1 = metrics([rec_5h_dominant])
        self.assertEqual(stats1.used_5h_percent, 75.0)
        self.assertEqual(stats1.used_week_percent, 30.0)
        self.assertEqual(stats1.used_percent, 75.0)
        if stats1.used_5h_percent is None or stats1.used_week_percent is None:
            raise AssertionError("Expected non-None used percentages")
        self.assertEqual(stats1.used_percent, max(stats1.used_5h_percent, stats1.used_week_percent))

        rec_week_dominant = UsageRecord(
            "run-2", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_after=LimitRemaining(five_hour_percent=85.0, week_percent=10.0), ts=NOW,
        )
        stats2 = metrics([rec_week_dominant])
        self.assertEqual(stats2.used_5h_percent, 15.0)
        self.assertEqual(stats2.used_week_percent, 90.0)
        self.assertEqual(stats2.used_percent, 90.0)
        if stats2.used_5h_percent is None or stats2.used_week_percent is None:
            raise AssertionError("Expected non-None used percentages")
        self.assertEqual(stats2.used_percent, max(stats2.used_5h_percent, stats2.used_week_percent))

        rec_5h_only = UsageRecord(
            "run-3", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_before=LimitRemaining(five_hour_percent=40.0, week_percent=None), ts=NOW,
        )
        stats3 = metrics([rec_5h_only])
        self.assertEqual(stats3.used_5h_percent, 60.0)
        self.assertIsNone(stats3.used_week_percent)
        self.assertEqual(stats3.used_percent, 60.0)

        rec_week_only = UsageRecord(
            "run-4", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_after=LimitRemaining(five_hour_percent=None, week_percent=45.0), ts=NOW,
        )
        stats4 = metrics([rec_week_only])
        self.assertIsNone(stats4.used_5h_percent)
        self.assertEqual(stats4.used_week_percent, 55.0)
        self.assertEqual(stats4.used_percent, 55.0)

        rec_none = UsageRecord(
            "run-5", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_before=LimitRemaining(five_hour_percent=None, week_percent=None), ts=NOW,
        )
        stats5 = metrics([rec_none])
        self.assertIsNone(stats5.used_5h_percent)
        self.assertIsNone(stats5.used_week_percent)
        self.assertIsNone(stats5.used_percent)

        json_data = stats1.to_json()
        self.assertEqual(json_data.get("used_5h_percent"), 75.0)
        self.assertEqual(json_data.get("used_week_percent"), 30.0)
        self.assertEqual(json_data.get("used_percent"), 75.0)

    def test_agy_record_with_limits_does_not_affect_codex_cell(self) -> None:
        codex_rec = UsageRecord(
            "run-codex", Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_before=LimitRemaining(five_hour_percent=60.0, week_percent=70.0), ts=NOW,
        )
        agy_rec = UsageRecord(
            "run-agy", Stage.IMPLEMENT, "reader", Engine.AGY, "gemini-flash", Effort.HIGH,
            TokenUsage(100, 0, 10, 0), 10, 0, "accepted", 1,
            limit_before=LimitRemaining(five_hour_percent=5.0, week_percent=5.0), ts=NOW,
        )
        stats_mixed = metrics([codex_rec, agy_rec])
        self.assertEqual(stats_mixed.used_5h_percent, 40.0)
        self.assertEqual(stats_mixed.used_week_percent, 30.0)
        self.assertEqual(stats_mixed.used_percent, 40.0)

        stats_agy = metrics([agy_rec])
        self.assertIsNone(stats_agy.used_5h_percent)
        self.assertIsNone(stats_agy.used_week_percent)
        self.assertIsNone(stats_agy.used_percent)

    def test_token_usage_without_cost_usd_gives_summary_cost_none(self) -> None:
        repo = make_repository(self.root)
        fake = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, fake)
        state = pipeline.start(make_inputs())
        self.assertEqual(state.stage, Stage.ACCEPT)
        result_file = pipeline.directory / "result.json"
        if not result_file.is_file():
            raise AssertionError("result.json does not exist")
        raw_result = loads(result_file.read_text(encoding="utf-8"))
        if not isinstance(raw_result, dict):
            raise AssertionError("Expected dict in result.json")
        summary = raw_result.get("usage_summary")
        if not isinstance(summary, dict):
            raise AssertionError("Expected dict in usage_summary")
        self.assertIn("cost_usd", summary)
        self.assertIsNone(summary["cost_usd"])

        # Also verify manual record addition with explicit cost_usd=None
        pipeline.usage.append(UsageRecord(
            pipeline.state.run_id, Stage.IMPLEMENT, "coder", Engine.CODEX, "gpt-worker", Effort.HIGH,
            TokenUsage(input_tokens=250, output_tokens=75, cost_usd=None),
            20, 0, "accepted", 1, ts=NOW,
        ))
        res = pipeline._result(Status.STOPPED)
        if not isinstance(res, dict):
            raise AssertionError("Expected dict result")
        direct_summary = res.get("usage_summary")
        if not isinstance(direct_summary, dict):
            raise AssertionError("Expected dict in direct_summary")
        self.assertIsNone(direct_summary.get("cost_usd"))
