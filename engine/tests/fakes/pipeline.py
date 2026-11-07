"""Offline adapters and fixtures shared by S2 integration and crash tests."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from reoengine._data import Json, atomic_write, loads, object_value
from reoengine.adapters.base import AgentResult, ErrorKind
from reoengine.config import Configuration, Engine, Limits, Profile, Project, RoleConfig
from reoengine.fsm import Pipeline, RunInputs
from reoengine.usage import TokenUsage


@dataclass(frozen=True)
class Call:
    model: str
    prompt: str
    cwd: Path
    empty: bool
    disable_skills: bool
    timeout: float


class FakeAdapter:
    def __init__(self, mode: str = "good", *, worktree: Path | None = None) -> None:
        self.mode = mode
        self.worktree = worktree
        self.calls: list[Call] = []

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        text = prompt_file.read_text(encoding="utf-8")
        parsed: object = json.JSONDecoder().raw_decode(text.split("DATA_JSON:\n", 1)[1])[0]
        from reoengine._data import json_value
        envelope = object_value(json_value(parsed), "envelope")
        data = object_value(envelope["data"], "data")
        model = role_cfg.model
        self.calls.append(Call(model, text, cwd, not any(cwd.iterdir()), role_cfg.disable_skills, timeout))
        response: Json = {"ok": True}
        if model == "quota":
            return AgentResult(29, "", TokenUsage(), 0.01, None, "quota", ErrorKind.QUOTA)
        if model.startswith("coder"):
            (cwd / "src/value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests/test_value.py").write_text(
                "import unittest\nfrom src.value import VALUE\n"
                "class ValueTest(unittest.TestCase):\n"
                "    def test_value(self):\n        self.assertEqual(VALUE, 1)\n", encoding="utf-8")
            if self.mode == "crash":
                os._exit(37)
            if self.mode == "interrupt":
                raise KeyboardInterrupt()
            if self.mode == "bad":
                (cwd / "src/value.py").write_text("VALUE = 0\n", encoding="utf-8")
            if self.mode == "large":
                (cwd / "src/large.txt").write_text("z" * 10000, encoding="utf-8")
            if self.mode == "outside":
                (cwd / "outside.txt").write_text("synthetic\n", encoding="utf-8")
            if self.mode == "secret":
                # Synthetic sentinel only; the engine must reject by name without opening it.
                (cwd / ".env.synthetic").touch()
            if self.mode == "excluded_secret":
                (cwd / ".env.example").touch()
            if self.mode == "rename":
                (cwd / "unchanged.txt").rename(cwd / "src/moved.txt")
            if self.mode == "link":
                external = cwd.parent / "synthetic-external"
                external.mkdir(exist_ok=True)
                link = cwd / "src/link"
                try:
                    link.symlink_to(external, target_is_directory=True)
                except OSError:
                    if os.name != "nt":
                        raise
                    import _winapi
                    _winapi.CreateJunction(str(external), str(link))
        elif model.startswith("reader"):
            task = object_value(data["task"], "task")
            requirements = task["requirements"]
            gates = object_value(data["gates"], "gates")
            response = {"checklist": [{"id": object_value(item, "req")["id"],
                                       "satisfied": True, "evidence": "synthetic test"}
                                      for item in requirements] if isinstance(requirements, list) else [],
                        "suspicious": [], "checks": gates["checks"], "unknowns": [],
                        "coverage": [{"path": path, "reviewed": True, "note": "synthetic review"}
                                     for path in data["changed_files"]]
                                    if isinstance(data["changed_files"], list) else []}
        elif model.startswith("reviewer"):
            if self.mode == "review_mutates" and self.worktree is not None:
                (self.worktree / "unchanged.txt").write_text("mutated", encoding="utf-8")
            response = {"approved": self.mode != "reject", "summary": "synthetic", "findings": []}
        elif model == "researcher":
            if self.mode == "research_mutates":
                (cwd / "unchanged.txt").write_text("mutated", encoding="utf-8")
            response = {"summary": "synthetic", "sources": [], "findings": [], "unknowns": []}
        elif model == "architect":
            response = make_inputs().plan
        output = prompt_file.parent / f"fake-{len(self.calls)}.json"
        atomic_write(output, response)
        return AgentResult(0, "", TokenUsage(10, 2, 3, 1), 0.01, output)


class SlotProbeAdapter:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.guard = threading.Lock()
        self.barrier = barrier
        self.active = 0
        self.maximum = 0

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        with self.guard:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=3)
            else:
                time.sleep(0.03)
        finally:
            with self.guard:
                self.active -= 1
        return AgentResult(0, "", TokenUsage(), 0.03, None)


def git_command(root: Path, *args: str) -> bytes:
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run(("git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                             "-c", "commit.gpgsign=false", *args), cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=False)
    return result.stdout


def make_repository(directory: Path) -> Path:
    root = directory / "repo"
    root.mkdir()
    git_command(root, "init", "-q")
    (root / "src").mkdir()
    (root / "src/value.py").write_text("VALUE = 0\n", encoding="utf-8")
    (root / "unchanged.txt").write_text("original\n", encoding="utf-8")
    git_command(root, "add", ".")
    git_command(root, "commit", "-qm", "synthetic base")
    return root


def make_inputs(*, tdd: bool = True) -> RunInputs:
    checks: list[Json] = ["unit-tests"]
    allowed: list[Json] = ["src/**", "tests/**"]
    return RunInputs({"id": "SYN-1", "title": "Synthetic", "description": "Set value to one",
                      "requirements": [{"id": "R1", "description": "Value is one", "verification": "test"}],
                      "allowed_paths": allowed, "acceptance_checks": checks, "profile": "synthetic",
                      "tdd_gate": tdd, **({"tdd_gate_reason": "synthetic docs test"} if not tdd else {})},
                     {"approach": "synthetic", "steps": [{"id": "one", "description": "change value", "role": None}],
                      "allowed_paths": allowed, "acceptance_checks": checks, "risks": []})


def make_project(root: Path, *, exclude_from_worktree: tuple[str, ...] = ()) -> Project:
    return Project(root, "synthetic", allowed_paths=("src/**", "tests/**"),
                   acceptance={"unit-tests": (sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests")},
                   exclude_from_worktree=exclude_from_worktree)


def make_pipeline(directory: Path, root: Path, adapter: FakeAdapter, *,
                  limits: Limits | None = None, profile: Profile | None = None,
                  config_directory: Path | None = None,
                  allow_legacy_argv_checks: bool = False,
                  exclude_from_worktree: tuple[str, ...] = ()) -> Pipeline:
    project = make_project(root, exclude_from_worktree=exclude_from_worktree)
    roles = {name: RoleConfig(Engine.CODEX, name) for name in
             ("coder", "reader", "reviewer", "researcher", "architect")}
    configuration = Configuration(roles, limits or Limits(), {"synthetic": profile or Profile()}, {"test": project})
    pipeline = Pipeline(configuration, project, directory / "run", directory / "state", {Engine.CODEX: adapter},
                        config_directory=config_directory, allow_legacy_argv_checks=allow_legacy_argv_checks)
    adapter.worktree = pipeline.git.root
    return pipeline
