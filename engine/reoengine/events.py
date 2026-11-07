"""Append-only per-run events; request and response contents never enter a record."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ._data import Json, encode, file_lock, loads, number, object_value
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with file_lock(fd):
                end = os.lseek(fd, 0, os.SEEK_END)
                if end:
                    os.lseek(fd, end - 1, os.SEEK_SET)
                    complete = os.read(fd, 1) == b"\n"
                else:
                    complete = True
                if not complete and not preserve_tail:
                    # Retain the established incomplete-tail recovery behavior.
                    from ._data import _recover_tail
                    _recover_tail(fd, self.path)
                    end = os.lseek(fd, 0, os.SEEK_END)
                    complete = True
                if end and complete:
                    tail_start = end - 1
                    while tail_start:
                        position = max(0, tail_start - 8192)
                        os.lseek(fd, position, os.SEEK_SET)
                        chunk = os.read(fd, tail_start - position)
                        newline = chunk.rfind(b"\n")
                        if newline >= 0:
                            tail_start = position + newline + 1
                            break
                        tail_start = position
                    os.lseek(fd, tail_start, os.SEEK_SET)
                    previous = os.read(fd, end - tail_start - 1)
                    try:
                        previous_value = object_value(loads(previous.decode("utf-8")), "event")
                    except (UnicodeError, ValueError):
                        previous_value = {}
                    previous_seq = previous_value.get("seq")
                    if type(previous_seq) is int and previous_seq >= 0:
                        seq = previous_seq + 1
                    else:
                        # Count the legacy prefix once, when introducing the chain.
                        os.lseek(fd, 0, os.SEEK_SET)
                        with os.fdopen(os.dup(fd), "rb") as stream:
                            seq = sum(1 for line in stream if line.endswith(b"\n"))
                    prev = hashlib.sha256(previous).hexdigest()
                else:
                    seq, prev = 0, "0" * 64
                data = event.to_json()
                data["seq"], data["prev"] = seq, prev
                payload = encode(data)
                os.lseek(fd, 0, os.SEEK_END)
                if not complete and preserve_tail:
                    payload = b"\n" + payload
                from ._data import _write_all
                _write_all(fd, payload)
                os.fsync(fd)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class ChainResult:
    ok: bool
    events: int
    index: int | None = None
    reason: str | None = None


def verify_chain(path: Path) -> ChainResult:
    previous = "0" * 64
    chained = False
    count = 0
    try:
        with path.open("rb") as stream:
            for index, raw in enumerate(stream):
                if not raw.endswith(b"\n"):
                    return ChainResult(False, count, index, "incomplete line")
                line = raw[:-1]
                try:
                    value = object_value(loads(line.decode("utf-8")), "event")
                except (UnicodeError, ValueError):
                    return ChainResult(False, count, index, "invalid JSON line")
                if "seq" not in value and "prev" not in value:
                    if chained:
                        return ChainResult(False, count, index, "legacy line after chain")
                    previous = hashlib.sha256(line).hexdigest()
                else:
                    chained = True
                    seq, prev = value.get("seq"), value.get("prev")
                    if type(seq) is not int or seq != index:
                        return ChainResult(False, count, index, "invalid sequence")
                    if not isinstance(prev, str) or prev != previous:
                        return ChainResult(False, count, max(0, index - 1), "previous hash mismatch")
                    previous = hashlib.sha256(line).hexdigest()
                count += 1
    except OSError as exc:
        return ChainResult(False, count, count, f"read error: {exc.__class__.__name__}")
    return ChainResult(True, count)
