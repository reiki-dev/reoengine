"""Monthly usage journals with cross-process append locks and a bounded date reader."""
from __future__ import annotations

import os

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ._data import (Json, append_line, file_lock, integer, keys, loads,
                    number, object_value, read_lines, string)
from .config import Effort, Engine, Pricing
from .events import LimitRemaining
from .state import Stage

Outcome = Literal["accepted", "gate_failed", "failed", "pending"]


@dataclass(frozen=True)
class TaskOutcome:
    run_id: str
    attempt: int
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    model_set: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Json]:
        if self.ts.utcoffset() is None:
            raise ValueError("TaskOutcome timestamp must be timezone-aware")
        string(self.run_id, "run_id")
        integer(self.attempt, "attempt", 1)
        cleaned_models: list[Json] = []
        for item in self.model_set:
            raw: Json = dict(item)
            candidate = object_value(raw, "model candidate")
            cleaned_models.append({
                "role": string(candidate.get("role"), "role"),
                "engine": string(candidate.get("engine"), "engine"),
                "model": string(candidate.get("model"), "model"),
                "effort": string(candidate.get("effort"), "effort"),
            })
        return {"run_id": self.run_id,
                "ts": self.ts.astimezone(timezone.utc).isoformat(),
                "attempt": self.attempt,
                "model_set": cleaned_models}


def _outcome(value: Json) -> TaskOutcome:
    data = object_value(value, "task outcome")
    run_id = string(data.get("run_id"), "run_id")
    attempt = integer(data.get("attempt"), "attempt", 1)
    ts = datetime.fromisoformat(string(data.get("ts"), "ts"))
    raw_model_set = data.get("model_set")
    model_set: list[dict[str, str]] = []
    if raw_model_set is not None:
        if not isinstance(raw_model_set, list):
            raise ValueError("model_set must be a list")
        for item in raw_model_set:
            candidate = object_value(item, "model candidate")
            model_set.append({
                "role": string(candidate.get("role"), "role"),
                "engine": string(candidate.get("engine"), "engine"),
                "model": string(candidate.get("model"), "model"),
                "effort": string(candidate.get("effort"), "effort"),
            })
    outcome = TaskOutcome(run_id, attempt, ts, model_set)
    outcome.to_json()
    return outcome


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    cost_usd: float | None = None
    duration_seconds: float | None = None
    cost_source: str | None = None

    def to_json(self) -> dict[str, Json]:
        data: dict[str, Json] = {"input_tokens": self.input_tokens, "cached_input_tokens": self.cached_input_tokens,
                                 "output_tokens": self.output_tokens,
                                 "reasoning_output_tokens": self.reasoning_output_tokens,
                                 "cost_usd": self.cost_usd, "duration_seconds": self.duration_seconds}
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
            if data[name] is not None:
                integer(data[name], name)
        for name in ("cost_usd", "duration_seconds"):
            if data[name] is not None:
                number(data[name], name)
        if self.cost_source not in (None, "reported", "computed"):
            raise ValueError("cost_source must be reported, computed, or None")
        data["cost_source"] = self.cost_source
        return data


def cost_usd(usage: TokenUsage, pricing: Pricing) -> float:
    input_tokens = usage.input_tokens or 0
    cached_tokens = usage.cached_input_tokens or 0
    output_tokens = usage.output_tokens or 0
    reasoning_tokens = usage.reasoning_output_tokens or 0
    return ((input_tokens - cached_tokens) * pricing.input
            + cached_tokens * pricing.cached_input_price
            + output_tokens * pricing.output
            + reasoning_tokens * pricing.reasoning_output_price) / 1_000_000


@dataclass(frozen=True)
class UsageRecord:
    """Usage record for an individual agent invocation (not overall task acceptance)."""
    run_id: str
    stage: Stage
    role: str
    engine: Engine
    model: str
    effort: Effort
    usage: TokenUsage
    duration_ms: int
    skill_count: int
    outcome: Outcome  # Outcome of an individual agent invocation, not task acceptance
    attempt: int
    limit_before: LimitRemaining | None = None
    limit_after: LimitRemaining | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> dict[str, Json]:
        if self.ts.utcoffset() is None:
            raise ValueError("Usage timestamp must be timezone-aware")
        integer(self.duration_ms, "duration_ms")
        integer(self.skill_count, "skill_count")
        integer(self.attempt, "attempt", 1)
        if self.outcome not in {"accepted", "gate_failed", "failed", "pending"}:
            raise ValueError("Unknown usage outcome")
        return {"ts": self.ts.astimezone(timezone.utc).isoformat(), "run_id": self.run_id,
                "stage": self.stage.value, "role": self.role, "engine": self.engine.value,
                "model": self.model, "effort": self.effort.value, **self.usage.to_json(),
                "duration_ms": self.duration_ms, "skill_count": self.skill_count,
                "outcome": self.outcome, "attempt": self.attempt,
                "limit_before": self.limit_before.to_json() if self.limit_before else None,
                "limit_after": self.limit_after.to_json() if self.limit_after else None}


def _limit(value: Json) -> LimitRemaining | None:
    if value is None:
        return None
    data = object_value(value, "limit")
    keys(data, {"five_hour_percent", "week_percent"}, "limit")
    def optional(key: str) -> float | None:
        item = data.get(key)
        return None if item is None else number(item, key)
    result = LimitRemaining(optional("five_hour_percent"), optional("week_percent"))
    result.to_json()
    return result


def _record(value: Json) -> UsageRecord:
    data = object_value(value, "usage")
    required = {"ts", "run_id", "stage", "role", "engine", "model", "effort", "input_tokens",
                "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "cost_usd", "duration_seconds",
                "duration_ms", "skill_count", "outcome", "attempt", "limit_before", "limit_after"}
    keys(data, required | {"cost_source"}, "usage")
    if required - data.keys():
        raise ValueError("usage: missing fields")
    def tokens(key: str) -> int | None:
        item = data.get(key)
        return None if item is None else integer(item, key)
    def cost(key: str) -> float | None:
        item = data.get(key)
        return None if item is None else number(item, key)
    outcome = string(data.get("outcome"), "outcome")
    if outcome not in ("accepted", "gate_failed", "failed", "pending"):
        raise ValueError("Unknown usage outcome")
    # Explicit branches preserve the Literal type at the untrusted JSON boundary.
    typed_outcome: Outcome = "accepted" if outcome == "accepted" else "gate_failed" if outcome == "gate_failed" else "failed" if outcome == "failed" else "pending"
    record = UsageRecord(string(data.get("run_id"), "run_id"), Stage(string(data.get("stage"), "stage")),
                         string(data.get("role"), "role"), Engine(string(data.get("engine"), "engine")),
                         string(data.get("model"), "model"), Effort(string(data.get("effort"), "effort")),
                         TokenUsage(tokens("input_tokens"), tokens("cached_input_tokens"), tokens("output_tokens"),
                                    tokens("reasoning_output_tokens"), cost("cost_usd"), cost("duration_seconds"),
                                    string(data["cost_source"], "cost_source") if data.get("cost_source") is not None else None),
                         integer(data.get("duration_ms"), "duration_ms"), integer(data.get("skill_count"), "skill_count"),
                         typed_outcome, integer(data.get("attempt"), "attempt", 1),
                         _limit(data.get("limit_before")), _limit(data.get("limit_after")),
                         datetime.fromisoformat(string(data.get("ts"), "ts")))
    record.to_json()
    return record


class UsageLog:
    def __init__(self, state_directory: Path) -> None:
        self.directory = state_directory

    def append(self, record: UsageRecord) -> Path:
        data = record.to_json()
        path = self.directory / f"usage-{record.ts.astimezone(timezone.utc):%Y-%m}.jsonl"
        append_line(path, data)
        return path

    def read_days(self, days: int = 14, *, now: datetime | None = None) -> list[UsageRecord]:
        if days <= 0:
            raise ValueError("days must be positive")
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        cutoff = current - timedelta(days=days)
        records: list[UsageRecord] = []
        for path in sorted(self.directory.glob("usage-????-??.jsonl")):
            month = path.stem.removeprefix("usage-")
            if cutoff.strftime("%Y-%m") <= month <= current.strftime("%Y-%m"):
                for value in read_lines(path):
                    record = _record(value)
                    if cutoff <= record.ts <= current:
                        records.append(record)
        return sorted(records, key=lambda record: record.ts)


class TaskOutcomeLog:
    def __init__(self, state_directory: Path) -> None:
        self.directory = state_directory
        self.path = state_directory / "task-outcomes.jsonl"

    def append(self, outcome: TaskOutcome) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / "task-outcomes.lock",
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with file_lock(fd):
                if self.path.exists():
                    with self.path.open("rb") as stream:
                        for line in stream:
                            if not line.endswith(b"\n") or not line.strip():
                                continue
                            try:
                                data = loads(line.decode("utf-8"))
                                if isinstance(data, dict) and data.get("run_id") == outcome.run_id:
                                    return False
                            except ValueError:
                                continue
                append_line(self.path, outcome.to_json())
                return True
        finally:
            os.close(fd)

    def read_all(self) -> list[TaskOutcome]:
        if not self.path.exists():
            return []
        return [_outcome(value) for value in read_lines(self.path)]
