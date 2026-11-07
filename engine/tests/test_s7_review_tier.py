from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_repository
from reoengine._data import Json, atomic_write, loads, object_value
from reoengine.config import Limits
from reoengine.fsm import StageError
from reoengine.gates import risk_tier


class ReviewTierAdapter(FakeAdapter):
    def __init__(self, *, changed_lines: int = 1, inflate_digest: bool = False) -> None:
        super().__init__()
        self.changed_lines = changed_lines
        self.inflate_digest = inflate_digest

    def invoke(self, role_cfg, prompt_file, cwd, timeout, schema=None):
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model.startswith("coder"):
            (cwd / "src/value.py").write_text("VALUE = 1\n" * self.changed_lines, encoding="utf-8")
        if role_cfg.model.startswith("reader") and self.inflate_digest and result.final_message_path is not None:
            digest = object_value(loads(result.final_message_path.read_text(encoding="utf-8")), "digest")
            checklist = digest["checklist"]
            if isinstance(checklist, list) and checklist:
                object_value(checklist[0], "checklist item")["evidence"] = "synthetic " * 3000
            atomic_write(result.final_message_path, digest)
        return result


class ReviewTierTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="s7-review-tier-", dir=ENGINE / "tests")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repository(self.root)

    def run_pipeline(self, *, changed_lines: int = 1, high_path: bool = False,
                     limits: Limits | None = None):
        adapter = ReviewTierAdapter(changed_lines=changed_lines)
        pipeline = make_pipeline(self.root, self.repo, adapter, limits=limits or Limits(max_fix_rounds=0))
        if high_path:
            pipeline.project = replace(pipeline.project, high_risk_paths=("src/**",))
        pipeline.start(make_inputs())
        return pipeline, adapter

    @staticmethod
    def reviewer_data(adapter: ReviewTierAdapter) -> dict[str, Json]:
        call = next(call for call in adapter.calls if call.model.startswith("reviewer"))
        parsed = json.JSONDecoder().raw_decode(call.prompt.split("DATA_JSON:\n", 1)[1])[0]
        envelope = object_value(parsed, "envelope")
        return object_value(envelope["data"], "reviewer data")

    def test_high_risk_path_sends_diff_and_records_full_diff_mode(self) -> None:
        pipeline, adapter = self.run_pipeline(high_path=True)
        data = self.reviewer_data(adapter)
        diff = (pipeline.directory / "review/diff.patch").read_bytes().decode("utf-8")
        self.assertEqual(data["diff"], diff)
        self.assertEqual(data["instruction"],
                         "Review the digest against the full diff; the diff is authoritative.")
        self.assertIn('Use only the supplied digest and diff.', next(
            call.prompt for call in adapter.calls if call.model.startswith("reviewer")))
        binding = object_value(loads((pipeline.directory / "review/binding.json").read_text()), "binding")
        self.assertEqual(binding["review_mode"], "full_diff")
        self.assertEqual(binding["review_reason"], "high_risk_path")

    def test_small_normal_diff_sends_digest_only(self) -> None:
        pipeline, adapter = self.run_pipeline()
        data = self.reviewer_data(adapter)
        self.assertNotIn("diff", data)
        self.assertIn("checklist", data)
        self.assertIn('Use only the supplied digest.', next(
            call.prompt for call in adapter.calls if call.model.startswith("reviewer")))
        binding = object_value(loads((pipeline.directory / "review/binding.json").read_text()), "binding")
        self.assertEqual(binding["review_mode"], "digest")
        self.assertEqual(binding["review_reason"], "normal")

    def test_diff_size_switches_normal_paths_to_full_diff(self) -> None:
        pipeline, adapter = self.run_pipeline(changed_lines=401)
        self.assertEqual(risk_tier(("src/value.py",), diff_patch=b"+x\n" * 401), "high")
        self.assertEqual(risk_tier(("src/value.py",), diff_patch=b"+x\n" * 400), "normal")
        data = self.reviewer_data(adapter)
        self.assertIn("diff", data)
        binding = object_value(loads((pipeline.directory / "review/binding.json").read_text()), "binding")
        self.assertEqual(binding["review_mode"], "full_diff")
        self.assertEqual(binding["review_reason"], "diff_size")

    def test_oversized_full_diff_fails_before_reviewer_invocation(self) -> None:
        adapter = ReviewTierAdapter(changed_lines=401, inflate_digest=True)
        pipeline = make_pipeline(self.root, self.repo, adapter, limits=Limits(max_fix_rounds=0))
        pipeline.project = replace(pipeline.project, high_risk_paths=("src/**",))
        original = __import__("reoengine.fsm", fromlist=["build_prompt"]).build_prompt

        def reject_full_diff(*args, **kwargs):
            if kwargs.get("json_contract") and not kwargs.get("read_only_full_diff"):
                prompt = original(*args, **{**kwargs, "limits": replace(kwargs["limits"], max_prompt_bytes=2_097_152)})
                size = len(prompt.text.encode("utf-8"))
                pipeline.config = replace(pipeline.config,
                                          limits=replace(pipeline.config.limits, max_prompt_bytes=size))
                return prompt
            return original(*args, **kwargs)

        with patch("reoengine.fsm.build_prompt", side_effect=reject_full_diff):
            pipeline.start(make_inputs())
        error = object_value(loads((pipeline.directory / "error.json").read_text()), "error")
        self.assertEqual(error["error"], "review_input_too_large")
        self.assertFalse(any(call.model.startswith("reviewer") for call in adapter.calls))


if __name__ == "__main__":
    unittest.main()
