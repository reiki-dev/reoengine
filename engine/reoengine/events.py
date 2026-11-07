"""Append-only per-run events; request and response contents never enter a record."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ._data import Json, append_line, encode, file_lock, number
from .config import Engine
from .state import Stage


@dataclass(frozen=True)
class LimitRemaining:
    five_hour_percent: float | None = None
    week_percent: float | None = None

    def to_json(self) -> dict[str, Json]:
        for value in (self.five_hour_percent, self.week_percent):
            if value is not None and number(value, "limit remaining") > 100:
                raise ValueError("Limit remaining must be between 0 and 100")
        return {"five_hour_percent": self.five_hour_percent, "week_percent": self.week_percent}


class EventKind(str, Enum):
    STAGE_START = "stage_start"
    STAGE_END = "stage_end"
    GATE_FAILED = "gate_failed"
    FALLBACK_USED = "fallback_used"
    ROLE_SWITCHED = "role_switched"
    RESUME = "resume"
    RATE_LIMITS_UNAVAILABLE = "rate_limits_unavailable"
    TDD_DISABLED = "tdd_disabled"
    AGENT_END = "agent_end"
    RESPONSE_VALIDATED = "response_validated"
    REVIEW_MODE = "review_mode"
    SOURCE_DIRTY = "source_dirty"
    OUTSIDE_WRITE_DETECTED = "outside_write_detected"
    WORKTREE_EXCLUSIONS = "worktree_exclusions"
    SOURCE_REPO_CONFIG_CHANGED = "source_repo_config_changed"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    TASK_ACCEPTED = "task_accepted"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Event:
    run_id: str
    event: EventKind
    stage: Stage | None = None
    role: str | None = None
    model: str | None = None
    engine: Engine | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    limit_remaining: LimitRemaining | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    partial_tree: bool | None = None
    response_attempt: int | None = None
    valid: bool | None = None
    note: str | None = None
    changed_file_count: int | None = None
    untracked_file_count: int | None = None
    category: str | None = None
    excluded_file_count: int | None = None
    old_repositoryformatversion: int | None = None
    new_repositoryformatversion: int | None = None
    worktreeConfig_enabled: bool | None = None

    def to_json(self) -> dict[str, Json]:
        for digest in (self.input_hash, self.output_hash):
            if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError("Event hashes must be SHA-256 hex digests")
        if self.ts.utcoffset() is None:
            raise ValueError("Event timestamp must be timezone-aware")
        if self.duration_ms is not None:
            number(self.duration_ms, "duration_ms")
        data: dict[str, Json] = {"ts": self.ts.astimezone(timezone.utc).isoformat(), "run_id": self.run_id,
                "stage": self.stage.value if self.stage else None, "role": self.role,
                "model": self.model, "engine": self.engine.value if self.engine else None,
                "exit_code": self.exit_code, "duration_ms": self.duration_ms,
                "input_hash": self.input_hash, "output_hash": self.output_hash,
                "limit_remaining": self.limit_remaining.to_json() if self.limit_remaining else None,
                "event": self.event.value}
        if self.partial_tree is not None:
            data["partial_tree"] = self.partial_tree
        if self.response_attempt is not None:
            data["response_attempt"] = self.response_attempt
            data["valid"] = self.valid
        if self.note is not None:
            data["note"] = self.note
        if self.changed_file_count is not None:
            data["changed_file_count"] = self.changed_file_count
        if self.untracked_file_count is not None:
            data["untracked_file_count"] = self.untracked_file_count
        if self.category is not None:
            data["category"] = self.category
        if self.excluded_file_count is not None:
            data["excluded_file_count"] = self.excluded_file_count
        if self.old_repositoryformatversion is not None:
            data["old_repositoryformatversion"] = self.old_repositoryformatversion
        if self.new_repositoryformatversion is not None:
            data["new_repositoryformatversion"] = self.new_repositoryformatversion
        if self.worktreeConfig_enabled is not None:
            data["worktreeConfig_enabled"] = self.worktreeConfig_enabled
        return data


class EventLog:
    def __init__(self, run_directory: Path) -> None:
        self.path = run_directory / "events.jsonl"

    def append(self, event: Event, *, preserve_tail: bool = False) -> None:
        if not preserve_tail:
            append_line(self.path, event.to_json())
            return
        # Preserve evidence even if an agent truncated/corrupted the old tail.
        # The separator ensures the detection record starts on its own line.
        with self.path.open("ab") as stream:
            with file_lock(stream.fileno()):
                stream.write(b"\n" + encode(event.to_json()))
                stream.flush()
                os.fsync(stream.fileno())
