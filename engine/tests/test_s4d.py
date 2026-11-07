from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_repository
from reona._data import Json, atomic_write, loads, object_value, read_lines, strings
from reona.adapters.base import AgentResult
from reona.cli import main
from reona.config import Engine, Limits, RoleConfig
from reona.fsm import RunInputs
from reona.schemas import SchemaError, load_schema, normalize_input_plan, validate, validate_artifact
from reona.state import Status, load_state


class SchemaContractTests(unittest.TestCase):
    def test_model_schemas_are_strict_recursively(self) -> None:
        def visit(schema: dict[str, Json], path: str) -> None:
            if "anyOf" in schema:
                alternatives = schema["anyOf"]
                self.assertIsInstance(alternatives, list, path)
                assert isinstance(alternatives, list)
                self.assertTrue(alternatives, path)
                for index, child in enumerate(alternatives):
                    visit(object_value(child, path), f"{path}.anyOf[{index}]")
            else:
                self.assertIn("type", schema, path)
            expected = schema.get("type")
            types = expected if isinstance(expected, list) else [expected]
            if "object" in types:
                properties = object_value(schema["properties"], path)
                required = strings(schema.get("required"), path)
                self.assertEqual(set(required), set(properties), path)
                self.assertIs(schema.get("additionalProperties"), False, path)
                for key, child in properties.items():
                    visit(object_value(child, path), f"{path}.{key}")
            if "array" in types:
                self.assertIn("items", schema, path)
                visit(object_value(schema["items"], path), f"{path}[]")

        for name in ("plan", "research", "review", "review-digest"):
            with self.subTest(schema=name):
                visit(load_schema(name), name)

    def test_nullable_role_is_required_in_model_plan(self) -> None:
        plan = make_inputs().plan
        assert plan is not None
        steps = plan["steps"]
        assert isinstance(steps, list)
        step = object_value(steps[0], "step")
        for role in (None, "coder"):
            step["role"] = role
            validate_artifact("plan", plan)
        step["role"] = 1
        with self.assertRaises(SchemaError):
            validate_artifact("plan", plan)
        del step["role"]
        with self.assertRaisesRegex(SchemaError, "role: required field missing"):
            validate_artifact("plan", plan)

    def test_nullable_line_is_required_in_model_review(self) -> None:
        finding: dict[str, Json] = {"path": "src/value.py", "line": None, "severity": "low", "text": "synthetic"}
        review: dict[str, Json] = {"approved": False, "summary": "synthetic", "findings": [finding]}
        for line in (None, 7):
            finding["line"] = line
            validate_artifact("review", review)
        for invalid in (True, "7", 1.5):
            finding["line"] = invalid
            with self.assertRaises(SchemaError):
                validate_artifact("review", review)
        del finding["line"]
        with self.assertRaisesRegex(SchemaError, "line: required field missing"):
            validate_artifact("review", review)

    def test_validator_null_unions_and_schema_checks(self) -> None:
        validate(None, {"type": "null"})
        for value in (None, 2):
            validate(value, {"type": ["integer", "null"]})
        validate(None, {"type": ["object", "null"], "properties": {"name": {"type": "string"}},
                        "required": ["name"], "additionalProperties": False})
        validate(None, {"type": ["array", "null"], "items": {"type": "integer"}})
        with self.assertRaises(SchemaError):
            validate(None, {"type": "integer"})
        with self.assertRaises(SchemaError):
            validate(None, {"type": ["integer", "null"], "enum": [2]})
        for invalid_types in ([], ["integer", "bogus"], ["null", None], ["null", ["string"]]):
            with self.subTest(types=invalid_types), self.assertRaises(SchemaError):
                validate({}, {"type": "object", "properties": {"unused": {"type": invalid_types}}})
        with self.assertRaisesRegex(SchemaError, "items schema required"):
            validate(None, {"type": ["array", "null"]})

    def test_input_normalization_only_supplies_missing_role_and_preserves_original(self) -> None:
        plan = make_inputs().plan
        assert plan is not None
        plan["steps"] = [{"id": "one", "description": "synthetic"},
                         {"id": "two", "description": "synthetic", "role": "coder"}]
        original = deepcopy(plan)
        normalized = normalize_input_plan(plan)
        self.assertEqual(plan, original)
        self.assertEqual(normalized["steps"], [{"role": None, "id": "one", "description": "synthetic"},
                                               {"role": "coder", "id": "two", "description": "synthetic"}])
        for invalid in ([{"id": "one"}], [None], [{"id": "one", "description": "synthetic", "role": 7}]):
            with self.subTest(steps=invalid), self.assertRaises(SchemaError):
                normalize_input_plan({**plan, "steps": invalid})


class FindingAdapter(FakeAdapter):
    def __init__(self, approved: bool) -> None:
        super().__init__()
        self.approved = approved

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model == "reviewer":
            assert result.final_message_path is not None
            atomic_write(result.final_message_path, {
                "approved": self.approved, "summary": "synthetic",
                "findings": [{"path": "src/value.py", "line": None, "severity": "low", "text": "synthetic finding"}]})
        return result


class PipelineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reona-s4d-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = make_repository(self.directory)
        self.config = self.directory / "custom-config"
        self.config.mkdir()

    def test_legacy_input_plan_runs_and_resumes_without_migration(self) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        inputs.plan["steps"] = [{"id": "one", "description": "synthetic"}]
        original = deepcopy(inputs.plan)
        fake = FakeAdapter()
        pipeline = make_pipeline(self.directory, self.repo, fake, config_directory=self.config)
        self.assertEqual(pipeline.start(inputs).status, Status.AWAITING_HUMAN)
        stored = (pipeline.directory / "inputs.json").read_bytes()
        self.assertEqual(object_value(loads(stored.decode("utf-8")), "inputs")["plan"], original)
        self.assertEqual(inputs.plan, original)
        plan = loads((pipeline.directory / "plan.json").read_text(encoding="utf-8"))
        validate_artifact("plan", plan)
        coder = next(call for call in fake.calls if call.model == "coder")
        self.assertIn('"role":null', coder.prompt)
        count = len(fake.calls)
        resumed = make_pipeline(self.directory, self.repo, fake, config_directory=self.config)
        self.assertEqual(resumed.resume().status, Status.AWAITING_HUMAN)
        self.assertEqual(len(fake.calls), count)
        self.assertEqual((pipeline.directory / "inputs.json").read_bytes(), stored)

    def test_result_and_fix_loop_preserve_null_lines(self) -> None:
        for approved in (True, False):
            with self.subTest(approved=approved):
                case = self.directory / str(approved)
                case.mkdir()
                fake = FindingAdapter(approved)
                pipeline = make_pipeline(case, self.repo, fake, limits=Limits(max_fix_rounds=1),
                                         config_directory=self.config)
                expected = Status.AWAITING_HUMAN if approved else Status.STOPPED
                self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, expected)
                result = object_value(loads((pipeline.directory / "result.json").read_text(encoding="utf-8")), "result")
                validate_artifact("result", result)
                review = object_value(result["review"], "review")
                self.assertEqual(review["findings"], [{"path": "src/value.py", "line": None,
                                                      "severity": "low", "text": "synthetic finding"}])
                summary = object_value(result["usage_summary"], "summary")
                self.assertIn("cost_usd", summary)
                del summary["cost_usd"]
                validate_artifact("result", result)
                if not approved:
                    coders = [call for call in fake.calls if call.model == "coder"]
                    self.assertEqual(len(coders), 2)
                    envelope = object_value(loads(coders[1].prompt.split("DATA_JSON:\n", 1)[1]), "envelope")
                    self.assertEqual(object_value(envelope["data"], "data")["feedback"], review["findings"])

    def test_cli_custom_config_detects_coder_write_with_legacy_input_plan(self) -> None:
        (self.config / "profiles").mkdir()
        (self.config / "projects").mkdir()
        roles = ''.join(f'[roles.{name}]\nengine="codex"\nmodel="{name}"\n'
                        for name in ("coder", "reader", "reviewer", "architect", "researcher"))
        (self.config / "roles.toml").write_text(roles, encoding="utf-8")
        target = self.config / "limits.toml"
        target.write_text("max_fix_rounds=1\n", encoding="utf-8")
        (self.config / "profiles/synthetic.toml").write_text('rules="synthetic"\n', encoding="utf-8")
        (self.config / "projects/synthetic.toml").write_text(
            f'repo="{self.repo.as_posix()}"\nprofile="synthetic"\n'
            'allowed_paths=["src/**", "tests/**"]\n[acceptance]\n'
            f'unit-tests={json.dumps([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])}\n',
            encoding="utf-8")
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        inputs.plan["steps"] = [{"id": "one", "description": "synthetic"}]
        task_path, plan_path = self.directory / "task.json", self.directory / "plan.json"
        atomic_write(task_path, inputs.task)
        atomic_write(plan_path, inputs.plan)
        original_plan = plan_path.read_bytes()
        fake = FakeAdapter()
        invoke = fake.invoke

        def write_config(role: RoleConfig, prompt: Path, cwd: Path, timeout: float,
                         schema: Path | None = None) -> AgentResult:
            result = invoke(role, prompt, cwd, timeout, schema)
            if role.model == "coder":
                target.write_text("max_fix_rounds=0\n", encoding="utf-8")
            return result

        out, err = io.StringIO(), io.StringIO()
        # Explicit CLI selection must win over a different environment/default directory.
        with patch.dict(os.environ, {"REONA_CONFIG_DIR": str(self.directory / "other-config")}), \
                patch.object(fake, "invoke", side_effect=write_config), \
                patch("reona.cli.codex_rate_limits", side_effect=AssertionError("No real probes")):
            code = main(["--config-dir", str(self.config), "run", "synthetic", "--task", str(task_path),
                         "--plan", str(plan_path)], root=self.directory, adapters={Engine.CODEX: fake}, out=out, err=err)
        self.assertEqual(code, 1, err.getvalue())
        run = next((self.directory / "runs").iterdir())
        self.assertEqual(load_state(run).status, Status.FAILED)
        error = object_value(loads((run / "error.json").read_text(encoding="utf-8")), "error")
        self.assertEqual(error["error"], "outside_write:config")
        self.assertIn("outside_write:config", out.getvalue())
        events = [object_value(value, "event") for value in read_lines(run / "events.jsonl")]
        detections = [event for event in events if event.get("event") == "outside_write_detected"]
        self.assertEqual([(event["category"], event["changed_file_count"]) for event in detections], [("config", 1)])
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        self.assertEqual(plan_path.read_bytes(), original_plan)


if __name__ == "__main__":
    unittest.main()
