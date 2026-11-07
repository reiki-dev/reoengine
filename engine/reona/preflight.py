"""Deterministic 14-day tuner. Decisions and configuration writes use no model."""
from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._data import (Json, append_line, atomic_write, file_lock, loads, number,
                    object_value, read_lines, string)
from .config import Configuration, Effort, Engine, RoleConfig, apply_runtime_tuning, effective_roles
from .gc import collect_failures
from .usage import TaskOutcome, TaskOutcomeLog, UsageLog, UsageRecord

WORKERS = frozenset({"researcher", "reader", "coder", "tester", "writer", "analyst",
                     "product", "designer", "security", "support", "legal", "release"})
EFFORTS = tuple(Effort)


def working_role(name: str, role: RoleConfig) -> bool:
    return name in WORKERS and not any(word in role.model.lower() for word in ("opus", "argon", "astra"))


@contextmanager
def settings_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / "settings.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd):
            yield
    finally:
        os.close(fd)


def read_tuning(directory: Path) -> dict[str, Json]:
    path = directory / "tuning.json"
    return object_value(loads(path.read_text(encoding="utf-8")), "tuning") if path.exists() else {"roles": {}}


@dataclass(frozen=True)
class Metrics:
    runs: int
    accepted: int
    first_rate: float
    gate_rate: float
    repeats: int
    tokens_per_accepted: float | None
    duration_ms: float
    used_percent: float | None
    used_5h_percent: float | None = None
    used_week_percent: float | None = None

    def to_json(self) -> dict[str, Json]:
        return {"runs": self.runs, "accepted": self.accepted, "first_rate": self.first_rate,
                "gate_rate": self.gate_rate, "repeats": self.repeats,
                "tokens_per_accepted": self.tokens_per_accepted, "duration_ms": self.duration_ms,
                "used_percent": self.used_percent,
                "used_5h_percent": self.used_5h_percent,
                "used_week_percent": self.used_week_percent}


def metrics(records: list[UsageRecord], outcomes: Sequence[TaskOutcome] = ()) -> Metrics:
    runs = {record.run_id for record in records}
    outcomes_by_run = {outcome.run_id: outcome for outcome in outcomes}
    accepted = runs & outcomes_by_run.keys()
    first: set[str] = set()
    for run in accepted:
        if outcomes_by_run[run].attempt == 1 and not any(
            record.run_id == run and record.outcome in ("failed", "gate_failed") for record in records
        ):
            first.add(run)
    rejected = {record.run_id for record in records if record.outcome == "gate_failed"}
    total = sum((record.usage.input_tokens or 0) + (record.usage.output_tokens or 0) for record in records)
    known = all(record.usage.input_tokens is not None and record.usage.output_tokens is not None for record in records)
    remaining_5h = [limit.five_hour_percent for record in records if record.engine == Engine.CODEX
                    for limit in (record.limit_before, record.limit_after)
                    if limit is not None and limit.five_hour_percent is not None]
    remaining_week = [limit.week_percent for record in records if record.engine == Engine.CODEX
                      for limit in (record.limit_before, record.limit_after)
                      if limit is not None and limit.week_percent is not None]
    used_5h = 100 - min(remaining_5h) if remaining_5h else None
    used_week = 100 - min(remaining_week) if remaining_week else None
    available = [v for v in (used_5h, used_week) if v is not None]
    used = max(available) if available else None
    return Metrics(len(runs), len(accepted), len(first) / len(runs) if runs else 0,
                   len(rejected) / len(runs) if runs else 0,
                   sum(max(record.attempt for record in records if record.run_id == run) - 1 for run in runs),
                   total / len(accepted) if accepted and known else None,
                   sum(record.duration_ms for record in records) / len(runs) if runs else 0,
                   used, used_5h, used_week)


@dataclass(frozen=True)
class Change:
    knob: str
    before: Json
    after: Json
    cell: tuple[str, str, str]
    baseline: Metrics
    reason: str
    rollback: bool = False

    def to_json(self, now: datetime) -> dict[str, Json]:
        return {"ts": now.isoformat(), "action": "rollback" if self.rollback else "apply",
                "knob": self.knob, "before": self.before, "after": self.after,
                "cell": list(self.cell), "metrics": self.baseline.to_json(), "reason": self.reason,
                "freeze_until": (now + timedelta(days=14)).isoformat() if self.rollback else None}


def _put(tuning: dict[str, Json], knob: str, value: Json) -> None:
    parts = knob.split(".")
    current = tuning
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = object_value(current[part], knob)
    if value is None:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = value


def _get(tuning: dict[str, Json], knob: str) -> Json:
    current: Json = tuning
    for part in knob.split("."):
        current = object_value(current, knob).get(part)
        if current is None:
            break
    return current


def preflight(configuration: Configuration, state_directory: Path, runs_directory: Path, *,
              apply: bool = False, summary: bool = False, now: datetime | None = None) -> list[str]:
    current = now or datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise ValueError("Preflight time must be timezone-aware")
    collect_failures(runs_directory, now=current)
    with settings_lock(state_directory):
        return _analyze(configuration, state_directory, apply, summary, current)


def _analyze(configuration: Configuration, directory: Path, apply: bool,
             summary: bool, now: datetime) -> list[str]:
    lines: list[str] = []
    try:
        outcomes = TaskOutcomeLog(directory).read_all()
    except ValueError:
        lines.append("acceptance log is corrupted: tuning skipped")
        return lines
    records = [record for record in UsageLog(directory).read_days(now=now) if record.outcome != "pending"]
    tuning = read_tuning(directory)
    roles = effective_roles(configuration.roles, directory / "tuning.json", directory / "overrides.json")
    runtime = apply_runtime_tuning(configuration, directory / "tuning.json")
    history_path = directory / "tuning-log.jsonl"
    history = [object_value(value, "tuning-log") for value in read_lines(history_path)] if history_path.exists() else []
    latest: dict[str, dict[str, Json]] = {}
    for item in history:
        latest[string(item.get("knob"), "knob")] = item
    changes: dict[str, Change] = {}
    frozen: set[str] = set()
    # A knob is observed for five distinct subsequent runs before another step.
    for knob, item in latest.items():
        freeze = item.get("freeze_until")
        if isinstance(freeze, str) and datetime.fromisoformat(freeze) > now:
            frozen.add(knob)
            lines.append(f"Frozen: {knob} until {freeze}")
            continue
        if item.get("action") != "apply":
            continue
        logged_cell = item.get("cell")
        if not isinstance(logged_cell, list) or len(logged_cell) != 3:
            raise ValueError("Invalid tuning-log cell")
        role, model, effort = (string(value, "cell") for value in logged_cell)
        if _get(tuning, knob) != item.get("after"):
            continue
        timestamp = datetime.fromisoformat(string(item.get("ts"), "ts"))
        expected_effort = string(item.get("after"), "after") if knob.endswith(".effort") else effort
        following = [record for record in records if record.ts > timestamp and record.role == role
                     and record.model == model and record.effort.value == expected_effort]
        run_ids = list(dict.fromkeys(record.run_id for record in following))[:5]
        frozen.add(knob)
        if len(run_ids) < 5:
            lines.append(f"Observing: {knob}, {len(run_ids)}/5 runs")
            continue
        observed = metrics([record for record in following if record.run_id in run_ids], outcomes)
        baseline = object_value(item.get("metrics"), "baseline")
        previous_tokens, tokens = baseline.get("tokens_per_accepted"), observed.tokens_per_accepted
        worse = observed.first_rate < number(baseline.get("first_rate"), "first_rate")
        grew = (previous_tokens is not None and tokens is not None
                and tokens > number(previous_tokens, "tokens_per_accepted") * 1.2)
        if worse or grew:
            changes[knob] = Change(knob, item.get("after"), item.get("before"), (role, model, effort),
                                   observed, "first-try acceptance declined or tokens per accepted task increased by >20% over 5 runs", True)
        else:
            frozen.remove(knob)
    cells: dict[tuple[str, str, str], list[UsageRecord]] = {}
    for record in records:
        cells.setdefault((record.role, record.model, record.effort.value), []).append(record)
    for cell, entries in sorted(cells.items()):
        name, model, effort = cell
        stats = metrics(entries, outcomes)
        lines.append(f"Cell {name}/{model}/{effort}: {stats.to_json()}")
        if stats.accepted < 10 or name not in roles:
            continue
        role_cfg = roles[name]
        if role_cfg.model != model or role_cfg.effort.value != effort:
            continue
        direction = 1 if stats.gate_rate > 0.3 else -1 if stats.first_rate >= 0.9 and EFFORTS.index(role_cfg.effort) >= EFFORTS.index(Effort.HIGH) else 0
        if direction:
            index = EFFORTS.index(role_cfg.effort) + direction
            if 0 <= index < len(EFFORTS):
                knob = f"roles.{name}.effort"
                if not working_role(name, role_cfg):
                    lines.append(f"Suggestion (manual only): {knob} -> {EFFORTS[index].value}")
                elif knob not in frozen and knob not in changes:
                    changes[knob] = Change(knob, _get(tuning, knob), EFFORTS[index].value, cell, stats,
                                           "gate failures >30%" if direction > 0 else "first-try acceptance >=90%")
            elif direction > 0:
                lines.append(f"Suggestion (manual only): next model for {name}")
        if (role_cfg.engine == Engine.CODEX and stats.used_percent is not None
                and stats.used_percent > 80 and name in ("reader", "researcher")):
            knob = "limits.slots.readers"
            count = runtime.limits.slots["readers"]
            if count > 1 and knob not in frozen and knob not in changes:
                changes[knob] = Change(knob, _get(tuning, knob), count - 1, cell, stats, "window usage >80%")
    for change in changes.values():
        lines.append(f"{'Rollback' if change.rollback else 'Apply' if apply else 'Dry-run'}: {change.knob} -> {change.after}; {change.reason}")
        if apply:
            _put(tuning, change.knob, change.after)
            append_line(history_path, change.to_json(now))
    if apply:
        atomic_write(directory / "tuning.json", tuning)
    if not changes:
        lines.append("no changes; insufficient data" if not cells or all(metrics(entries, outcomes).accepted < 10 for entries in cells.values())
                     else "no changes")
    if summary:
        lines.append("Summary: placeholder interface; no model is called.")
    return lines
