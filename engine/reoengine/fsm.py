"""Checkpointed pipeline. No operation commits, pushes, merges, or accepts code.

An interrupted stage is repeated in full; completed artifacts are content checked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
from pathlib import Path

from ._data import Json, atomic_write, encode, loads, object_value, read_lines, string, strings
from .adapters.base import Adapter, AgentResult, ErrorKind
from .adapters.claude import _validate_neutral_directory
from .config import Configuration, Engine, Profile, Project, RoleConfig, Sandbox, check_identifier
from .events import Event, EventKind, EventLog, LimitRemaining, sha256_file
from .gates import GateRequest, bounded_tail, plant_canary, risk_tier, run_gates, sanitize_output
from .gitops import CANARY_PATH, Diff, Git, SafetyError, ensure_safe_tree, normalized_path, tree_hash
from .guard import Category, OutsideGuard, SnapshotError
from .jsonout import extract_json
from .prompt import build_prompt
from .runlinks import check_run_links, ensure_run_links
from .schemas import SCHEMA_DIRECTORY, normalize_input_plan, validate_artifact, validate_file
from .skills import select_skills
from .slots import Slots, WriterLockTimeout
from .state import RunLock, RunState, Stage, Status, load_state, save_state
from .usage import Outcome, TaskOutcome, TaskOutcomeLog, UsageLog, UsageRecord, _record


class StageError(ValueError):
    pass


class LimitExhausted(StageError):
    pass


class InvalidDigest(StageError):
    pass


class OutsideWrite(StageError):
    """Stop immediately, preserving the agent's writes for human inspection."""

    def __init__(self, category: Category, *, preserve_state: bool = False) -> None:
        super().__init__(f"outside_write:{category}")
        self.preserve_state = preserve_state


class TransientStageError(StageError):
    pass


def is_transient_failure(exc: BaseException) -> bool:
    if isinstance(exc, OutsideWrite):
        return False
    if isinstance(exc, (LimitExhausted, WriterLockTimeout, TransientStageError)):
        return True
    if isinstance(exc, InvalidDigest):
        return False
    if isinstance(exc, StageError):
        parts = str(exc).split(":")
        return len(parts) == 3 and parts[0] == "agent_failed" and bool(parts[1]) and parts[2] == "timeout"
    return False


@dataclass(frozen=True)
class RunInputs:
    task: dict[str, Json]
    plan: dict[str, Json] | None = None
    research: bool = False


from .policy import _commands, _digest, _diff_path, _path_segments, _within_paths
from .review_policy import (_diff_sections, _effective_review_approved, _excerpt_in_section,
                            _incomplete_review, _line_in_section, _review_approved, review_input_digest)
from .stages.accept import AcceptMixin
from .stages.gates_fix import GatesFixMixin
from .stages.review import ReviewMixin

class Pipeline(ReviewMixin, AcceptMixin, GatesFixMixin):
    def __init__(self, configuration: Configuration, project: Project, run_directory: Path,
                 state_directory: Path, adapters: Mapping[Engine, Adapter], *,
                 skill_catalog: Mapping[str, str] | None = None, slots: Slots | None = None,
                 role_provider: Callable[[], Mapping[str, RoleConfig]] | None = None,
                 rate_limits: Callable[[], LimitRemaining | None] | None = None,
                 config_directory: Path | None = None,
                 allow_legacy_argv_checks: bool = False) -> None:
        self.config = configuration
        self.project = project
        self.directory = run_directory.resolve()
        self.state_directory = state_directory.resolve()
        self.source = Git(project.repo, exclude_from_worktree=project.exclude_from_worktree)
        default_config = Path(__file__).resolve().parents[2] / "config"
        self.config_directory = (config_directory if config_directory is not None else
                                 Path(os.environ.get("REOENGINE_CONFIG_DIR", str(default_config)))).resolve()
        self.guard = OutsideGuard(self.directory, self.config_directory, self.state_directory, self.source)
        for path in (self.directory, self.state_directory):
            if path.is_relative_to(self.source.root):
                raise ValueError("Run and state directories must be outside the project repository")
        self.git = Git(self.directory / "worktree", exclude_from_worktree=project.exclude_from_worktree)
        self.adapters = adapters
        self.catalog = skill_catalog or {}
        self.slots = slots or Slots(configuration.limits, self.state_directory)
        self.role_provider = role_provider or (lambda: configuration.roles)
        self.rate_limits = rate_limits
        self.allow_legacy_argv_checks = allow_legacy_argv_checks
        self.events = EventLog(self.directory)
        self.usage = UsageLog(self.state_directory)
        self.outcomes = TaskOutcomeLog(self.state_directory)
        self.state = RunState(self.directory.name)
        self.inputs = RunInputs({})
        self.profile = Profile()
        self.exit_code = 0
        self._stage_roles: Mapping[str, RoleConfig] | None = None

    def _event(self, event: EventKind, *, role: str | None = None,
               cfg: RoleConfig | None = None, exit_code: int | None = None,
               input_hash: str | None = None, output_hash: str | None = None,
               duration_ms: int | None = None, partial_tree: bool | None = None,
               response_attempt: int | None = None, valid: bool | None = None,
               changed_file_count: int | None = None, untracked_file_count: int | None = None,
               category: str | None = None, preserve_tail: bool = False,
               excluded_file_count: int | None = None,
               note: str | None = None) -> None:
        event_note = note if note is not None else ("quota_shared_with_chat" if cfg and cfg.engine == Engine.CLAUDE else None)
        self.events.append(Event(self.state.run_id, event, self.state.stage, role,
                                 cfg.model if cfg else None, cfg.engine if cfg else None,
                                 exit_code, duration_ms, input_hash, output_hash, partial_tree=partial_tree,
                                 response_attempt=response_attempt, valid=valid,
                                 note=event_note,
                                 changed_file_count=changed_file_count, untracked_file_count=untracked_file_count,
                                 category=category, excluded_file_count=excluded_file_count), preserve_tail=preserve_tail)

    def _save(self, *, stage: Stage | None = None, status: Status | None = None,
              attempt: int | None = None, base_sha: str | None = None,
              head_sha: str | None = None, input_hashes: dict[str, str] | None = None,
              output_hashes: dict[str, str] | None = None, tree_hash: str | None = None) -> None:
        self.state = replace(self.state, stage=stage if stage is not None else self.state.stage,
                             status=status if status is not None else self.state.status,
                             attempt=attempt if attempt is not None else self.state.attempt,
                             base_sha=base_sha if base_sha is not None else self.state.base_sha,
                             head_sha=head_sha if head_sha is not None else self.state.head_sha,
                             input_hashes=input_hashes if input_hashes is not None else self.state.input_hashes,
                             output_hashes=output_hashes if output_hashes is not None else self.state.output_hashes,
                             tree_hash=tree_hash if tree_hash is not None else self.state.tree_hash)
        save_state(self.directory, self.state)

    def start(self, inputs: RunInputs) -> RunState:
        with RunLock(self.directory):
            if (self.directory / "state.json").exists():
                raise ValueError("Run exists; use resume")
            self._set_inputs(inputs)
            atomic_write(self.directory / "inputs.json", {"task": inputs.task, "plan": inputs.plan,
                                                          "research": inputs.research,
                                                          "exclude_from_worktree": self._exclusions()})
            self.state = replace(self.state, input_hashes={"run_inputs": sha256_file(self.directory / "inputs.json")})
            save_state(self.directory, self.state)
            return self._drive()

    def _exclusions(self) -> list[Json]:
        return list(sorted(map(normalized_path, self.project.exclude_from_worktree)))

    def _set_inputs(self, inputs: RunInputs) -> None:
        validate_artifact("task", inputs.task)
        if inputs.plan is not None:
            inputs = replace(inputs, plan=normalize_input_plan(inputs.plan))
        profile_name = string(inputs.task.get("profile"), "profile")
        if profile_name != self.project.profile or profile_name not in self.config.profiles:
            raise ValueError("Task/project profile mismatch")
        self.profile = self.config.profiles[profile_name]
        self.inputs = inputs

    def _verify_run_integrity(self, *, action: str) -> dict[str, Json]:
        inputs_path = self.directory / "inputs.json"
        if not inputs_path.is_file():
            raise StageError(f"{action} refused: run inputs missing")
        if self.state.input_hashes.get("run_inputs") != sha256_file(inputs_path):
            raise StageError(f"{action} refused: run inputs changed")
        for key, expected_hash in self.state.output_hashes.items():
            name, attempt = key.split(":")
            artifact = self.directory / "checkpoints" / f"{Stage(name).value}-{int(attempt)}.json"
            if not artifact.is_file() or sha256_file(artifact) != expected_hash:
                raise StageError(f"{action} refused: completed stage artifact changed or missing")
        data = object_value(loads(inputs_path.read_text(encoding="utf-8")), "inputs")
        if data.get("exclude_from_worktree", []) != self._exclusions():
            raise StageError(f"{action} refused: exclude_from_worktree configuration changed")
        return data

    def _verify_worktree(self, *, action: str = "resume") -> bool:
        if self.git.root.exists():
            ensure_safe_tree(self.git.root, self.git.exclude_from_worktree)
            implement_done = f"implement:{self.state.attempt}" in self.state.output_hashes
            after_implement = self.state.stage in (Stage.GATES, Stage.REVIEW, Stage.FIX_LOOP, Stage.ACCEPT, Stage.DONE)
            dependent_results = any(f"{stage.value}:{self.state.attempt}" in self.state.output_hashes
                                    for stage in (Stage.GATES, Stage.REVIEW, Stage.ACCEPT))
            unfinished_implement = (self.state.stage == Stage.IMPLEMENT and not implement_done
                                    and not dependent_results)
            same_head = self.git.head() == self.state.head_sha
            same_tree = tree_hash(self.git.root, self.git.exclude_from_worktree) == self.state.tree_hash
            if ((implement_done or after_implement or dependent_results) and not same_tree
                    or not same_head and not same_tree):
                raise StageError(f"{action} refused: worktree differs from checkpoint tree hash")
            return unfinished_implement and same_head
        if self.state.base_sha and self.state.stage != Stage.PREFLIGHT:
            raise StageError(f"{action} refused: worktree missing")
        return False

    def _reset_implement_worktree(self) -> None:
        ensure_safe_tree(self.git.root, self.git.exclude_from_worktree)
        self.source.command(("worktree", "remove", "--force", str(self.git.root)))
        self._prepare()
        self._save(head_sha=self.git.head(), tree_hash=self._tree_stamp())

    def resume(self, *, remove_stale: bool = False, retry_failed: bool = False) -> RunState:
        with RunLock(self.directory, remove_stale=remove_stale):
            self.state = load_state(self.directory)
            data = self._verify_run_integrity(action="resume")
            plan = None if data["plan"] is None else object_value(data["plan"], "plan")
            self._set_inputs(RunInputs(object_value(data["task"], "task"), plan, data["research"] is True))
            self._validate_policy()
            if plan is None and (self.directory / "plan.json").is_file():
                self._resolve_checks(self._plan()["acceptance_checks"], "plan.acceptance_checks")
            partial_tree = self._verify_worktree(action="resume")
            ensure_run_links(self.directory, self.project.links)
            if retry_failed and self.state.status == Status.FAILED and self.state.stage == Stage.IMPLEMENT:
                if self.state.attempt != 1:
                    raise StageError("resume refused: retry-failed is supported only for the first implement attempt")
                error_path = self.directory / "error.json"
                if not error_path.is_file():
                    raise StageError("resume refused: failure is not transient")
                try:
                    error_data = loads(error_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise StageError("resume refused: failure is not transient")
                if not isinstance(error_data, dict):
                    raise StageError("resume refused: failure is not transient")
                if error_data.get("stage") != Stage.IMPLEMENT.value or error_data.get("transient") is not True:
                    raise StageError("resume refused: failure is not transient")

                retries_path = self.directory / "retries.json"
                retries_data: dict[str, Json] = {}
                n = 0
                if retries_path.exists():
                    try:
                        raw = loads(retries_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        raise StageError("resume refused: retries.json is corrupt")
                    if not isinstance(raw, dict):
                        raise StageError("resume refused: retries.json is corrupt")
                    retries_data = raw
                    val = retries_data.get("implement:1", 0)
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                        raise StageError("resume refused: retries.json is corrupt")
                    n = val

                if n >= 2:
                    raise StageError("resume refused: implement retry budget exhausted (2)")

                retries_data["implement:1"] = n + 1
                atomic_write(retries_path, retries_data)
                self._reset_implement_worktree()
                (self.directory / "error.json").unlink(missing_ok=True)
                self._save(status=Status.RUNNING)
                self.exit_code = 0
                self._event(EventKind.RESUME, note=f"retry_failed:implement:{n + 1}", partial_tree=False)
                return self._drive()

            if self.state.status == Status.FAILED and self.state.stage == Stage.REVIEW:
                required = (Stage.IMPLEMENT, Stage.GATES)
                if any(f"{stage.value}:{self.state.attempt}" not in self.state.output_hashes for stage in required):
                    raise StageError("resume refused: review prerequisites missing")
                if self._load(Stage.GATES).get("passed") is not True:
                    raise StageError("resume refused: review requires passed gates")
                self._save(status=Status.RUNNING)
                self.exit_code = 0
                (self.directory / "error.json").unlink(missing_ok=True)
            self._event(EventKind.RESUME, partial_tree=partial_tree)
            return self._drive()

    def _artifact(self, stage: Stage) -> Path:
        return self.directory / "checkpoints" / f"{stage.value}-{self.state.attempt}.json"

    def _load(self, stage: Stage) -> dict[str, Json]:
        return object_value(loads(self._artifact(stage).read_text(encoding="utf-8")), stage.value)

    def _plan(self) -> dict[str, Json]:
        if self.inputs.plan is not None:
            return self.inputs.plan
        return object_value(validate_file("plan", self.directory / "plan.json"), "plan")

    def _allowed(self) -> tuple[str, ...]:
        return strings(self.inputs.task["allowed_paths"], "allowed_paths")

    def _resolve_checks(self, value: Json, where: str, *, required: bool = True) -> tuple[tuple[str, ...], ...]:
        if not isinstance(value, list) or required and not value:
            raise StageError(f"{where}: nonempty check ID array required")
        if value and all(isinstance(item, list) for item in value):
            if not self.allow_legacy_argv_checks:
                raise StageError(f"{where}: legacy argv checks disabled; use registered IDs or --allow-legacy-argv-checks")
            legacy_commands = _commands(value)
            events = read_lines(self.events.path) if self.events.path.exists() else []
            if not any(isinstance(event, dict) and event.get("run_id") == self.state.run_id
                       and event.get("note") == "legacy_argv_checks" and event.get("category") == where
                       for event in events):
                self.events.append(Event(self.state.run_id, EventKind.RESPONSE_VALIDATED, self.state.stage,
                                         valid=True, note="legacy_argv_checks", category=where))
            return legacy_commands
        seen: set[str] = set()
        commands: list[tuple[str, ...]] = []
        for item in value:
            if not isinstance(item, str):
                raise StageError(f"{where}: expected string check ID")
            check_identifier(item)
            if item in seen:
                raise StageError(f"{where}: duplicate check ID {item!r}")
            if item not in self.project.acceptance:
                raise StageError(f"{where}: unknown check ID {item!r}")
            seen.add(item)
            commands.append(self.project.acceptance[item])
        return tuple(commands)

    def _task_checks(self) -> tuple[tuple[str, ...], ...]:
        return self._resolve_checks(self.inputs.task["acceptance_checks"], "task.acceptance_checks",
                                    required=self.inputs.task["tdd_gate"] is True
                                    or not (self.project.checks or self.profile.checks))

    def _validate_plan_paths(self, plan: Mapping[str, Json]) -> None:
        allowed = strings(plan["allowed_paths"], "allowed_paths")
        if not allowed or any(path not in self._allowed() for path in allowed):
            raise StageError("Plan must use a nonempty subset of task allowed_paths")
        parents = tuple(_path_segments(path) for path in self.project.allowed_paths)
        for path in allowed:
            parts = _path_segments(path)
            if not any(_within_paths(parts, parent) for parent in parents):
                raise StageError(f"Plan allowed_paths outside project allowed_paths: {path!r}")

    def _validate_policy(self) -> None:
        parents = tuple(_path_segments(path) for path in self.project.allowed_paths)
        allowed = self._allowed()
        if not parents or not allowed:
            raise StageError("Task allowed_paths requires nonempty project allowed_paths")
        for path in allowed:
            parts = _path_segments(path)
            if not any(_within_paths(parts, parent) for parent in parents):
                raise StageError(f"Task allowed_paths outside project allowed_paths: {path!r}")
        self._task_checks()
        if self.inputs.plan is not None or (self.directory / "plan.json").is_file():
            self._validate_plan_paths(self._plan())
        if self.inputs.plan is not None:
            self._resolve_checks(self.inputs.plan["acceptance_checks"], "plan.acceptance_checks")

    def _checks(self) -> tuple[tuple[str, ...], ...]:
        return tuple(dict.fromkeys((*self.project.checks, *self.profile.checks,
                                    *self._task_checks(),
                                    *self._resolve_checks(self._plan()["acceptance_checks"], "plan.acceptance_checks"))))

    def _next(self, stage: Stage) -> Stage | None:
        if stage == Stage.CREATED:
            return Stage.PREFLIGHT
        if stage == Stage.PREFLIGHT:
            return Stage.BRIEF
        if stage == Stage.BRIEF:
            return Stage.RESEARCH if self.inputs.research else Stage.PLAN
        if stage == Stage.RESEARCH:
            return Stage.PLAN
        if stage == Stage.PLAN:
            return Stage.IMPLEMENT
        if stage == Stage.IMPLEMENT:
            return Stage.GATES
        if stage == Stage.GATES:
            return Stage.REVIEW if self._load(stage)["passed"] is True else Stage.FIX_LOOP
        if stage == Stage.REVIEW:
            review = self._load(stage)
            if not _review_approved(review):
                return Stage.FIX_LOOP
            digest, reason = self._review_verification()
            if reason is not None:
                self._review_feedback(review, reason)
                return Stage.FIX_LOOP
            return Stage.ACCEPT if digest is not None and _effective_review_approved(review, digest) else Stage.FIX_LOOP
        if stage == Stage.FIX_LOOP:
            return Stage.IMPLEMENT
        return None

    def _drive(self) -> RunState:
        if self.state.status in (Status.AWAITING_HUMAN, Status.STOPPED, Status.FAILED) or self.state.stage == Stage.DONE:
            return self.state
        stage = self.state.stage if self.state.status == Status.RUNNING else self._next(self.state.stage)
        while stage is not None:
            try:
                self.execute_stage(stage)
            except (OSError, ValueError, RuntimeError) as exc:
                self.exit_code = 3 if isinstance(exc, LimitExhausted) else 1
                atomic_write(self.directory / "error.json", {"stage": stage.value, "error": str(exc),
                                                            "exit_code": self.exit_code,
                                                            "transient": is_transient_failure(exc)})
                if isinstance(exc, OutsideWrite) and exc.preserve_state:
                    # Do not overwrite a damaged state/artifact with our copy.
                    self.state = replace(self.state, status=Status.FAILED)
                else:
                    self._save(status=Status.FAILED)
                self._event(EventKind.STAGE_END, exit_code=self.exit_code, preserve_tail=isinstance(exc, OutsideWrite))
                self._flush_coder_usage("failed")
                self._result(Status.FAILED, trust_artifacts=not isinstance(exc, OutsideWrite))
                return self.state
            if self.state.status in (Status.AWAITING_HUMAN, Status.STOPPED):
                return self.state
            stage = self._next(stage)
        return self.state

    def execute_stage(self, stage: Stage) -> dict[str, Json]:
        """Execute or reuse one explicit stage. Caller must hold the run lock.

        Stage inputs include configuration and artifacts, so changing either cannot
        reuse a stale result.
        """
        if stage == Stage.IMPLEMENT and self.state.attempt == 0:
            self._save(attempt=1)
        self._stage_roles = dict(self.role_provider())
        payload: dict[str, Json] = {"task": self.inputs.task, "plan": self.inputs.plan,
                                    "base": self.state.base_sha, "attempt": self.state.attempt,
                                    "profile": repr(self.profile), "project": repr(self.project),
                                    "limits": repr(self.config.limits), "skills": dict(self.catalog),
                                    "roles": repr(dict(self._stage_roles)),
                                    "allow_legacy_argv_checks": self.allow_legacy_argv_checks}
        if stage in (Stage.IMPLEMENT, Stage.GATES, Stage.REVIEW, Stage.FIX_LOOP, Stage.ACCEPT):
            payload["plan"] = self._plan()
        if stage in (Stage.GATES, Stage.REVIEW, Stage.ACCEPT):
            payload["tree"] = self._tree_stamp()
        if stage == Stage.IMPLEMENT:
            payload["feedback"] = self._feedback()
            payload["research"] = self._research()
        elif stage == Stage.PLAN:
            payload["research"] = self._research()
        input_hash = _digest(payload)
        key = f"{stage.value}:{self.state.attempt}"
        artifact = self._artifact(stage)
        if (self.state.input_hashes.get(key) == input_hash and artifact.exists()
                and self.state.output_hashes.get(key) == sha256_file(artifact)):
            return object_value(loads(artifact.read_text(encoding="utf-8")), stage.value)
        self._save(stage=stage, status=Status.RUNNING)
        self._event(EventKind.STAGE_START, input_hash=input_hash)
        try:
            output = self._perform(stage)
        except WriterLockTimeout as exc:
            raise TransientStageError(str(exc)) from exc
        atomic_write(artifact, output)
        inputs, outputs = dict(self.state.input_hashes), dict(self.state.output_hashes)
        inputs[key], outputs[key] = input_hash, sha256_file(artifact)
        status = (Status.AWAITING_HUMAN if stage == Stage.ACCEPT else Status.STOPPED
                  if output.get("status") == "stopped" else Status.COMPLETED)
        next_attempt = self.state.attempt + 1 if stage == Stage.FIX_LOOP and status != Status.STOPPED else self.state.attempt
        self._save(status=status, attempt=next_attempt,
                   input_hashes=inputs, output_hashes=outputs,
                   head_sha=self.git.head(), tree_hash=self._tree_stamp())
        stop_reason = output.get("stop_reason")
        self._event(EventKind.STAGE_END, input_hash=input_hash, output_hash=outputs[key], exit_code=0,
                    note=stop_reason if isinstance(stop_reason, str) else None)
        return output

    def _tree_stamp(self) -> str:
        try:
            return tree_hash(self.git.root, self.git.exclude_from_worktree)
        except SafetyError:
            # Retain a fail-closed checkpoint without reading a prohibited file;
            # the paths gate can now emit its structured refusal.
            return "unsafe"

    def _research(self) -> dict[str, Json] | None:
        path = self.directory / "research.json"
        if not self.inputs.research or not path.exists():
            return None
        return object_value(loads(path.read_text(encoding="utf-8")), "research")

    def _feedback(self) -> list[Json]:
        path = self.directory / "feedback.json"
        if not path.exists():
            return []
        data = loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Invalid feedback")
        return data

    def _perform(self, stage: Stage) -> dict[str, Json]:
        if stage == Stage.PREFLIGHT:
            return self._prepare()
        if stage == Stage.BRIEF:
            atomic_write(self.directory / "task.json", self.inputs.task)
            return self.inputs.task
        if stage == Stage.RESEARCH:
            research = self._invoke("researcher", {"task": self.inputs.task}, "research")
            atomic_write(self.directory / "research.json", research)
            return research
        if stage == Stage.PLAN:
            plan = self.inputs.plan or self._invoke("architect",
                                                    {"task": self.inputs.task, "research": self._research()},
                                                    "plan", neutral=True)
            validate_artifact("plan", plan)
            self._validate_plan_paths(plan)
            self._resolve_checks(plan["acceptance_checks"], "plan.acceptance_checks")
            atomic_write(self.directory / "plan.json", plan)
            return plan
        if stage == Stage.IMPLEMENT:
            return self._invoke("coder", {"task": self.inputs.task, "plan": self._plan(),
                                           "feedback": self._feedback(), "research": self._research()}, None)
        if stage == Stage.GATES:
            return self._gates()
        if stage == Stage.REVIEW:
            return self._review()
        if stage == Stage.FIX_LOOP:
            return self._fix()
        if stage == Stage.ACCEPT:
            return self._result(Status.AWAITING_HUMAN)
        raise StageError(f"Unsupported executable stage {stage.value}")

    def _prepare(self) -> dict[str, Json]:
        self._validate_policy()
        self.source.check_exclusion_support()
        if not self.state.base_sha:
            base = self.source.head()
            # Inspect only committed names. Never walk/open the source worktree:
            # its ignored and untracked secrets/data must stay outside this run.
            self.source.validate_base(base)
            changed, untracked = self.source.status_counts()
            if changed or untracked:
                self._event(EventKind.SOURCE_DIRTY, changed_file_count=changed, untracked_file_count=untracked)
            self._save(base_sha=base, head_sha=base)
        if not self.git.root.exists():
            before_config = self.source.source_config_state() if self.git.exclude_from_worktree else None
            try:
                self.source.create_worktree(self.git.root, self.state.base_sha)
            finally:
                # Engine-owned setup runs outside agent guard intervals. Each
                # agent's next snapshot includes this shared config as baseline.
                if before_config is not None:
                    after_config = self.source.source_config_state()
                    if before_config != after_config:
                        self.events.append(Event(
                            self.state.run_id, EventKind.SOURCE_REPO_CONFIG_CHANGED, self.state.stage,
                            category="source_repo_config", old_repositoryformatversion=before_config[0],
                            new_repositoryformatversion=after_config[0], worktreeConfig_enabled=after_config[1]))
            if self.git.exclude_from_worktree:
                self._event(EventKind.WORKTREE_EXCLUSIONS,
                            excluded_file_count=len(self.git.exclude_from_worktree))
        ensure_run_links(self.directory, self.project.links)
        if not (self.git.root / CANARY_PATH).exists():
            plant_canary(self.git)
        limits = self._limits()
        if limits is None:
            self._event(EventKind.RATE_LIMITS_UNAVAILABLE)
        return {"base_sha": self.state.base_sha, "limit_remaining": limits.to_json() if limits else None}

    def _limits(self) -> LimitRemaining | None:
        if self.rate_limits is None:
            return None
        try:
            return self.rate_limits()
        except (OSError, ValueError, RuntimeError):
            self._event(EventKind.RATE_LIMITS_UNAVAILABLE)
            return None

    def _invoke(self, role: str, data: Json, schema: str | None, *, neutral: bool = False,
                validator: Callable[[dict[str, Json]], None] | None = None,
                read_only_full_diff: bool = False) -> dict[str, Json]:
        error: str | None = None
        for response_attempt in range(1, 3 if schema == "review-digest" else 2):
            try:
                return self._invoke_once(role, data, schema, neutral=neutral,
                                         response_attempt=response_attempt, validation_error=error, validator=validator,
                                         read_only_full_diff=read_only_full_diff)
            except InvalidDigest as exc:
                error = str(exc)
        raise StageError("invalid_response:reader:two_validation_failures")

    def _invoke_once(self, role: str, data: Json, schema: str | None, *, neutral: bool,
                     response_attempt: int, validation_error: str | None,
                     validator: Callable[[dict[str, Json]], None] | None,
                     read_only_full_diff: bool = False) -> dict[str, Json]:
        ensure_safe_tree(self.git.root, self.git.exclude_from_worktree)
        check_run_links(self.directory, self.project.links)
        cfg = (self._stage_roles if self._stage_roles is not None else self.role_provider())[role]
        cfg = replace(cfg, disable_skills=self.profile.disable_skills,
                      sandbox=Sandbox.WORKSPACE_WRITE if role == "coder" else Sandbox.READ_ONLY)
        selected = select_skills(self.profile, self.catalog)
        if self.project.links and role == "coder":
            data = {**object_value(data, "coder request"),
                    "linked_directories": {name: "../" + name for name in self.project.links},
                    "instruction": "Project directory links are already prepared beside the worktree, at the supplied relative paths. "
                                   "Use these dependencies for checks; do not recreate, install or modify them."}
        prompt = build_prompt(data, base_sha=self.state.base_sha, allowed_paths=self._allowed(),
                              profile=self.profile, limits=self.config.limits, skills=selected,
                              read_only=role == "reviewer", json_contract=schema == "review-digest",
                              read_only_full_diff=read_only_full_diff,
                              validation_error=validation_error)
        prompt_path = self.directory / "prompts" / f"{self.state.stage.value}-{self.state.attempt}-{role}.txt"
        prompt.write(prompt_path)
        schema_path = SCHEMA_DIRECTORY / f"{schema}.json" if schema else None
        before = tree_hash(self.git.root, self.git.exclude_from_worktree)
        candidates = (cfg, *cfg.replacements())
        for index, candidate in enumerate(candidates):
            if index:
                self._event(EventKind.FALLBACK_USED, role=role, cfg=candidate)
            limit_before = self._limits() if candidate.engine == Engine.CODEX else None
            if candidate.engine == Engine.CODEX and limit_before is not None and any(
                    value is not None and value <= 0
                    for value in (limit_before.five_hour_percent, limit_before.week_percent)):
                if index + 1 < len(candidates):
                    continue
                raise LimitExhausted("Codex limit exhausted")
            with self.slots.acquire(role, self.state.run_id, writing=role == "coder"):
                if neutral or candidate.neutral_cwd:
                    with tempfile.TemporaryDirectory(prefix="reoengine-neutral-") as directory:
                        cwd = Path(directory)
                        _validate_neutral_directory(cwd)
                        if any((parent / ".git").exists() for parent in cwd.parents):
                            raise StageError("Neutral directory is inside a project")
                        result = self._guarded_invoke(candidate, prompt_path, cwd, schema_path, role=role)
                else:
                    result = self._guarded_invoke(candidate, prompt_path, self.git.root, schema_path, role=role)
            limit_after = self._limits() if candidate.engine == Engine.CODEX else None
            try:
                unchanged = role == "coder" or tree_hash(self.git.root, self.git.exclude_from_worktree) == before
            except (OSError, ValueError):
                unchanged = False
            response: dict[str, Json] | None = None
            invalid: ValueError | OSError | None = None
            # Adapters may reject the model's checks before the engine can
            # replace them. Revalidate the saved response after substitution;
            # every other schema/semantic constraint still has to pass.
            digest_schema_response = (schema == "review-digest" and result.error == ErrorKind.SCHEMA
                                      and result.exit_code == 0 and result.final_message_path is not None)
            if (result.success or digest_schema_response) and unchanged:
                try:
                    if result.final_message_path is None:
                        raise StageError("Missing agent response")
                    if schema == "review-digest":
                        try:
                            extracted = extract_json(result.final_message_path.read_text(encoding="utf-8"))
                            response = object_value(extracted, schema)
                            for field in ("risk_tier", "input_digest", "changed_files"):
                                response.pop(field, None)
                            self._supply_digest_checks(response)
                            if validator is not None:
                                validator(response)
                            validate_artifact(schema, response)
                        except (OSError, ValueError):
                            self._preserve_reader_tail(result, response_attempt)
                            raise
                        finally:
                            result.final_message_path.unlink(missing_ok=True)
                    else:
                        response = ({"response_hash": sha256_file(result.final_message_path)} if schema is None
                                    else object_value(validate_file(schema, result.final_message_path), schema))
                except (OSError, ValueError) as exc:
                    invalid = exc
                    response = None
            if schema == "review-digest" and result.error == ErrorKind.SCHEMA and not digest_schema_response:
                invalid = ValueError("Agent response does not match review-digest schema")
                self._preserve_reader_tail(result, response_attempt)
                if result.final_message_path is not None:
                    result.final_message_path.unlink(missing_ok=True)
            record = UsageRecord(self.state.run_id, self.state.stage, role, candidate.engine,
                                 candidate.model, candidate.effort, result.usage,
                                 int(result.duration * 1000), prompt.skill_count,
                                 ("gate_failed" if schema == "review" and response is not None
                                  and not _review_approved(response) else "accepted") if response is not None else "failed",
                                 max(1, self.state.attempt, response_attempt), limit_before, limit_after)
            if role == "coder" and response is not None:
                # Keep a durable metadata receipt until deterministic gates know
                # the outcome. Distinct receipts also retain calls repeated after
                # a crash; token usage is never counted twice during resume.
                atomic_write(self.directory / "usage" / f"coder-{self.state.attempt}-{uuid.uuid4().hex}.json",
                             replace(record, outcome="pending").to_json())
            else:
                self.usage.append(record)
            self._event(EventKind.AGENT_END, role=role, cfg=candidate, exit_code=result.exit_code,
                        duration_ms=int(result.duration * 1000), input_hash=sha256_file(prompt_path))
            if schema == "review-digest" and (result.success or digest_schema_response or invalid is not None):
                self._event(EventKind.RESPONSE_VALIDATED, role=role, cfg=candidate,
                            response_attempt=response_attempt, valid=response is not None,
                            category="review_digest_validation" if response is None else None)
            if not unchanged:
                raise StageError(f"{role}_tree_changed")
            if invalid is not None:
                if schema == "review-digest":
                    # Only engine-authored semantic messages are safe for retry feedback.
                    category = str(invalid) if isinstance(invalid, InvalidDigest) else "review-digest schema validation failed"
                    raise InvalidDigest(category) from invalid
                raise StageError(f"invalid_response:{role}") from invalid
            if result.success or response is not None:
                if response is None:
                    raise StageError("Missing response")
                return response
            if result.error == ErrorKind.QUOTA and index + 1 < len(candidates):
                continue
            # Keep only bounded failure tails with an explicit expiry.
            tail = bounded_tail(result.stdout_tail + result.stderr_tail, 4096)
            atomic_write(self.directory / "fail" / f"{self.state.stage.value}-{self.state.attempt}.json",
                         {"tail": tail, "expires_at": (datetime.now(timezone.utc) + timedelta(days=self.config.limits.failure_ttl_days)).isoformat()})
            error_type = LimitExhausted if result.error == ErrorKind.QUOTA else StageError
            raise error_type(f"agent_failed:{role}:{result.error.value if result.error else result.exit_code}")
        raise StageError("No adapter candidates")

    def _preserve_reader_tail(self, result: AgentResult, response_attempt: int) -> None:
        tail = bounded_tail(result.stdout_tail + result.stderr_tail, 4096)
        if result.final_message_path is not None:
            try:
                with result.final_message_path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 4096))
                    tail = stream.read(4096).decode("utf-8", errors="ignore")
            except OSError:
                pass
        atomic_write(self.directory / "fail" / f"reader-{self.state.attempt}-{response_attempt}-{uuid.uuid4().hex}.json",
                     {"tail": tail, "expires_at": (datetime.now(timezone.utc) + timedelta(days=self.config.limits.failure_ttl_days)).isoformat()})

    def _guarded_invoke(self, candidate: RoleConfig, prompt: Path, cwd: Path,
                        schema: Path | None, *, role: str = "") -> AgentResult:
        try:
            before = self.guard.snapshot()
            try:
                return self.adapters[candidate.engine].invoke(candidate, prompt, cwd,
                                                              self.config.limits.timeouts.get(role, self.config.limits.timeout_seconds), schema)
            finally:
                after = self.guard.snapshot()
                violations = self.guard.compare(before, after)
                for violation in violations:
                    self._event(EventKind.OUTSIDE_WRITE_DETECTED, category=violation.category,
                                changed_file_count=violation.changed_path_count, preserve_tail=True)
                if violations:
                    raise OutsideWrite(violations[0].category, preserve_state=(
                        before.files["engine_state"].get("state.json") != after.files["engine_state"].get("state.json")))
        except SnapshotError as exc:
            self._event(EventKind.OUTSIDE_WRITE_DETECTED, category=exc.category,
                        changed_file_count=1, preserve_tail=True)
            raise OutsideWrite(exc.category, preserve_state=exc.category == "engine_state") from None
