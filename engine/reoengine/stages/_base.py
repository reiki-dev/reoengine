"""Typed interface shared by pipeline stage mixins."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from ..fsm import RunInputs

from .._data import Json
from ..adapters.base import AgentResult
from ..config import Configuration, Profile, Project, RoleConfig
from ..events import EventKind, EventLog
from ..gitops import Diff, Git
from ..state import RunState, Stage, Status
from ..usage import Outcome, TaskOutcomeLog, UsageLog

class _Inputs(Protocol):
    @property
    def task(self) -> dict[str, Json]: ...
    @property
    def plan(self) -> dict[str, Json] | None: ...
    @property
    def research(self) -> bool: ...

class StageContext:
    config: Configuration
    project: Project
    directory: Path
    state: RunState
    inputs: _Inputs
    profile: Profile
    git: Git
    events: EventLog
    usage: UsageLog
    outcomes: TaskOutcomeLog
    catalog: Mapping[str, str]

    def _event(self, event: EventKind, *, role: str | None = None, cfg: RoleConfig | None = None,
               exit_code: int | None = None, input_hash: str | None = None, output_hash: str | None = None,
               duration_ms: int | None = None, partial_tree: bool | None = None,
               response_attempt: int | None = None, valid: bool | None = None,
               changed_file_count: int | None = None, untracked_file_count: int | None = None,
               category: str | None = None, preserve_tail: bool = False,
               excluded_file_count: int | None = None, note: str | None = None) -> None: raise NotImplementedError
    def _artifact(self, stage: Stage) -> Path: raise NotImplementedError
    def _load(self, stage: Stage) -> dict[str, Json]: raise NotImplementedError
    def _allowed(self) -> tuple[str, ...]: raise NotImplementedError
    def _checks(self) -> tuple[tuple[str, ...], ...]: raise NotImplementedError
    def _task_checks(self) -> tuple[tuple[str, ...], ...]: raise NotImplementedError
    def _plan(self) -> dict[str, Json]: raise NotImplementedError
    def _feedback(self) -> list[Json]: raise NotImplementedError
    def _invoke(self, role: str, data: Json, stage_name: str | None, *,
                validator: Callable[[dict[str, Json]], None] | None = None, neutral: bool = False,
                read_only_full_diff: bool = False) -> dict[str, Json]: raise NotImplementedError
    def _review_binding(self, diff: Diff) -> dict[str, Json]: raise NotImplementedError
    def _review_verification(self) -> tuple[dict[str, Json] | None, str | None]: raise NotImplementedError
    def _supply_digest_checks(self, digest: dict[str, Json]) -> None: raise NotImplementedError
    def _result(self, status: Status, *, trust_artifacts: bool = True,
                stop_reason: str | None = None) -> dict[str, Json]: raise NotImplementedError
    def _save(self, *, stage: Stage | None = None, status: Status | None = None,
              attempt: int | None = None, base_sha: str | None = None, head_sha: str | None = None,
              input_hashes: dict[str, str] | None = None, output_hashes: dict[str, str] | None = None,
              tree_hash: str | None = None) -> None: raise NotImplementedError
    def _verify_run_integrity(self, *, action: str) -> dict[str, Json]: raise NotImplementedError
    def _verify_worktree(self, *, action: str = "resume") -> bool: raise NotImplementedError
    def _set_inputs(self, inputs: RunInputs) -> None: raise NotImplementedError
    def _record_acceptance(self) -> None: raise NotImplementedError
    def _flush_coder_usage(self, outcome: Outcome) -> None: raise NotImplementedError
