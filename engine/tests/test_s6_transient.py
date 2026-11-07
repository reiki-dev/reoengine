"""Tests for transient failure classification and error.json reporting."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_project, make_repository
from reona.config import Configuration, Engine, Limits, Profile, RoleConfig
from reona.fsm import (
    InvalidDigest,
    LimitExhausted,
    OutsideWrite,
    Pipeline,
    StageError,
    is_transient_failure,
)
from reona.gitops import SafetyError
from reona.slots import WriterLockTimeout
from reona.state import Status


class CustomOutsideWrite(OutsideWrite):
    """Subclass of OutsideWrite carrying a timeout-like error message."""

    def __init__(self, msg: str = "agent_failed:coder:timeout") -> None:
        super().__init__("source_repo")
        self.args = (msg,)


class TransientClassificationTests(unittest.TestCase):
    def test_table_expected_values(self) -> None:
        cases: list[tuple[str, BaseException, bool]] = [
            # (a) LimitExhausted is always transient
            ("limit_exhausted_quota", LimitExhausted("agent_failed:coder:quota"), True),
            ("limit_exhausted_generic", LimitExhausted("limit exhausted"), True),

            # (b) WriterLockTimeout is always transient
            ("writer_lock_timeout", WriterLockTimeout("slots locked"), True),

            # (c) StageError with exact "agent_failed:<role>:timeout"
            ("stage_error_coder_timeout", StageError("agent_failed:coder:timeout"), True),
            ("stage_error_reviewer_timeout", StageError("agent_failed:reviewer:timeout"), True),
            ("stage_error_researcher_timeout", StageError("agent_failed:researcher:timeout"), True),
            ("stage_error_architect_timeout", StageError("agent_failed:architect:timeout"), True),

            # OutsideWrite and any subclass is strictly non-transient (checked first)
            ("outside_write_standard", OutsideWrite("source_repo"), False),
            ("outside_write_subclass_with_timeout_msg", CustomOutsideWrite("agent_failed:coder:timeout"), False),

            # Other agent failure kinds are non-transient
            ("stage_error_agent_process", StageError("agent_failed:coder:process"), False),
            ("stage_error_agent_protocol", StageError("agent_failed:coder:protocol"), False),
            ("stage_error_agent_schema", StageError("agent_failed:coder:schema"), False),
            ("stage_error_agent_numeric_exit_code_1", StageError("agent_failed:coder:1"), False),
            ("stage_error_agent_numeric_exit_code_137", StageError("agent_failed:coder:137"), False),

            # False matches / malformed messages
            ("false_match_prefix", StageError("xagent_failed:coder:timeout"), False),
            ("false_match_extra_field", StageError("agent_failed:coder:timeout:extra"), False),
            ("false_match_empty_role", StageError("agent_failed::timeout"), False),
            ("false_match_leading_field", StageError("prefix:agent_failed:coder:timeout"), False),
            ("false_match_two_fields", StageError("agent_failed:timeout"), False),
            ("false_match_substring_only", StageError("error with agent_failed:coder:timeout inside"), False),

            # Other exceptions are non-transient
            ("safety_error", SafetyError("unsafe outside write"), False),
            ("invalid_digest_standard", InvalidDigest("review_digest_too_large"), False),
            ("invalid_digest_timeout_msg", InvalidDigest("agent_failed:coder:timeout"), False),
            ("stage_error_generic", StageError("boom"), False),
            ("os_error", OSError("disk read error"), False),
            ("value_error", ValueError("invalid argument"), False),
            ("runtime_error", RuntimeError("generic runtime failure"), False),
            ("base_exception", BaseException("agent_failed:coder:timeout"), False),
            ("standard_exception", Exception("agent_failed:coder:timeout"), False),
        ]

        for name, exc, expected in cases:
            with self.subTest(case=name):
                self.assertIs(is_transient_failure(exc), expected)


class PipelineTransientIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s6-transient-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_pipeline_coder_quota_is_transient(self) -> None:
        repo = make_repository(self.root)
        project = make_project(repo)
        roles = {name: RoleConfig(Engine.CODEX, "quota" if name == "coder" else name)
                 for name in ("coder", "reader", "reviewer", "researcher", "architect")}
        cfg = Configuration(roles, Limits(), {"synthetic": Profile()}, {"test": project})
        adapter = FakeAdapter()
        pipeline = Pipeline(cfg, project, self.root / "run", self.root / "state", {Engine.CODEX: adapter})

        state = pipeline.start(make_inputs())

        self.assertEqual(state.status, Status.FAILED)
        error_file = pipeline.directory / "error.json"
        self.assertTrue(error_file.exists())
        payload = json.loads(error_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["exit_code"], 3)
        self.assertIs(payload["transient"], True)

    def test_pipeline_nontransient_failure(self) -> None:
        repo = make_repository(self.root)
        adapter = FakeAdapter()
        pipeline = make_pipeline(self.root, repo, adapter)

        with patch.object(pipeline, "execute_stage", side_effect=StageError("boom")):
            state = pipeline.start(make_inputs())

        self.assertEqual(state.status, Status.FAILED)
        error_file = pipeline.directory / "error.json"
        self.assertTrue(error_file.exists())
        payload = json.loads(error_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["exit_code"], 1)
        self.assertEqual(payload["error"], "boom")
        self.assertIs(payload["transient"], False)


if __name__ == "__main__":
    unittest.main()
