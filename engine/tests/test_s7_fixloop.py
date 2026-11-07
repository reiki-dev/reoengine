from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_project, make_repository
from reona._data import loads, object_value, read_lines
from reona.adapters.base import AgentResult
from reona.config import Limits, RoleConfig
from reona.fsm import Pipeline, RunInputs
from reona.state import Status


class FixAdapter(FakeAdapter):
    def __init__(self, *, progress: bool) -> None:
        super().__init__(mode="bad")
        self.progress = progress
        self.coder_calls = 0

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model.startswith("coder"):
            self.coder_calls += 1
            value = self.coder_calls if self.progress else 0
            (cwd / "src/value.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        return result


class FixLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE / "tests", prefix="s7-fix-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repository(self.root)

    def pipeline(self, adapter: FixAdapter, limits: Limits) -> Pipeline:
        project = make_project(self.repo)
        if adapter.progress:
            project = project.__class__(
                project.repo, project.profile,
                allowed_paths=project.allowed_paths,
                acceptance={
                    "first": (sys.executable, "-c", "from pathlib import Path; assert int(Path('src/value.py').read_text().split('=')[1]) >= 2"),
                    "second": (sys.executable, "-c", "from pathlib import Path; assert int(Path('src/value.py').read_text().split('=')[1]) >= 3"),
                },
                exclude_from_worktree=project.exclude_from_worktree)
        pipeline = make_pipeline(self.root, self.repo, adapter, limits=limits)
        pipeline.project = project
        pipeline.config = pipeline.config.__class__(pipeline.config.roles, limits,
                                                     pipeline.config.profiles, {"test": project})
        adapter.worktree = pipeline.git.root
        return pipeline

    def inputs(self, *, progress: bool = False) -> RunInputs:
        inputs = make_inputs()
        if not progress:
            return inputs
        checks = ["first", "second"]
        task = dict(inputs.task, acceptance_checks=checks)
        plan = dict(inputs.plan, acceptance_checks=checks)
        return inputs.__class__(task, plan, inputs.research)

    def test_identical_diff_and_nondecreasing_failures_stops_on_second_round(self) -> None:
        adapter = FixAdapter(progress=False)
        pipeline = self.pipeline(adapter, Limits())
        state = pipeline.start(self.inputs())
        self.assertEqual(state.status, Status.STOPPED)
        self.assertEqual(adapter.coder_calls, 2)
        result = object_value(loads((pipeline.directory / "result.json").read_text(encoding="utf-8")), "result")
        self.assertEqual(result["stop_reason"], "no_progress")
        stage_ends = [object_value(event, "event") for event in read_lines(pipeline.events.path)
                      if isinstance(event, dict) and event.get("event") == "stage_end"]
        self.assertTrue(any(event.get("note") == "no_progress" for event in stage_ends))

    def test_decreasing_failure_count_continues_fix_loop(self) -> None:
        adapter = FixAdapter(progress=True)
        pipeline = self.pipeline(adapter, Limits())
        state = pipeline.start(self.inputs(progress=True))
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        self.assertEqual(adapter.coder_calls, 3)

    def test_token_budget_stops_fix_loop(self) -> None:
        adapter = FixAdapter(progress=False)
        pipeline = self.pipeline(adapter, Limits(fix_loop_token_budget=1))
        state = pipeline.start(self.inputs())
        self.assertEqual(state.status, Status.STOPPED)
        self.assertEqual(adapter.coder_calls, 1)
        result = object_value(loads((pipeline.directory / "result.json").read_text(encoding="utf-8")), "result")
        self.assertEqual(result["stop_reason"], "fix_loop_budget")

    def test_disabled_no_progress_stops_at_max_fix_rounds(self) -> None:
        adapter = FixAdapter(progress=False)
        pipeline = self.pipeline(adapter, Limits(max_fix_rounds=3, fix_loop_no_progress_stop=False))
        state = pipeline.start(self.inputs())
        self.assertEqual(state.status, Status.STOPPED)
        self.assertEqual(adapter.coder_calls, 4)
        result = object_value(loads((pipeline.directory / "result.json").read_text(encoding="utf-8")), "result")
        self.assertEqual(result["stop_reason"], "max_fix_rounds")


if __name__ == "__main__":
    unittest.main()
