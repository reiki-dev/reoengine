"""Command line entry point with injectable processes, adapters and directories."""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from ._data import Json, atomic_write, json_value, loads, object_value, read_lines, string
from .adapters.base import Adapter
from .adapters.codex import codex_rate_limits
from .config import (Configuration, Effort, Engine, RoleConfig, effective_roles,
                     load_configuration)
from .doctor import Probe, diagnose
from .events import Event, EventKind, EventLog, LimitRemaining
from .factory import make_adapters
from .fsm import Pipeline, RunInputs, StageError
from .gc import collect_failures, collect_run_links, usage_size_warnings
from .preflight import preflight, settings_lock
from .schemas import normalize_input_plan, validate_artifact
from .state import RunLockedError, RunState, Status, load_state

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Directories:
    config: Path
    state: Path
    runs: Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="reona")
    for name in ("config", "state", "runs"):
        result.add_argument(f"--{name}-dir", help=f"Override {name}/ (or REONA_{name.upper()}_DIR)")
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a task in a separate worktree")
    run.add_argument("project")
    run.add_argument("--task", required=True)
    run.add_argument("--plan")
    run.add_argument("--research", action="store_true")
    run.add_argument("--roles", help="Use a roles TOML file (relative to config directory or absolute)")
    run.add_argument("--allow-legacy-argv-checks", action="store_true")
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--remove-stale", action="store_true", help="Explicitly reclaim a dead PID lock")
    resume.add_argument("--retry-failed", action="store_true", help="Retry the failed implement stage")
    resume.add_argument("--roles", help="Use a roles TOML file (relative to config directory or absolute)")
    resume.add_argument("--allow-legacy-argv-checks", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id", nargs="?")
    approve = commands.add_parser("approve", help="Approve an awaiting_human run")
    approve.add_argument("run_id")
    reject = commands.add_parser("reject", help="Reject an awaiting_human run")
    reject.add_argument("run_id")
    reject.add_argument("--reason", required=True, help="Reason for rejection")
    switch = commands.add_parser("switch")
    switch.add_argument("role", nargs="?")
    switch.add_argument("model", nargs="?")
    switch.add_argument("--effort", choices=[effort.value for effort in Effort])
    switch.add_argument("--engine", choices=[engine.value for engine in Engine])
    switch.add_argument("--reset", metavar="ROLE")
    switch.add_argument("--roles", help="Use a roles TOML file (relative to config directory or absolute)")
    tuning = commands.add_parser("preflight")
    mode = tuning.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    tuning.add_argument("--summary", action="store_true")
    commands.add_parser("gc")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--roles", help="Use a roles TOML file (relative to config directory or absolute)")
    return result


def _identifier(value: Json, where: str) -> str:
    identifier = string(value, where)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identifier) is None:
        raise ValueError(f"{where}: invalid identifier")
    return identifier


def _run_path(runs: Path, identifier: Json) -> Path:
    path = runs / _identifier(identifier, "run_id")
    if path.is_symlink() or not path.resolve().is_relative_to(runs.resolve()):
        raise ValueError("run_id escapes runs directory")
    return path


def _roles_file(value: Json, config_directory: Path) -> Path | None:
    if value is None:
        return None
    path = Path(string(value, "roles"))
    resolved = path if path.is_absolute() else config_directory / path
    if not resolved.is_file():
        raise ValueError(f"roles file not found: {resolved}")
    return resolved


def _configuration(directories: Directories, roles_file: Path | None = None) -> Configuration:
    return load_configuration(directories.config, directories.state, roles_file)


def _switch(args: dict[str, Json], directories: Directories, out: TextIO) -> int:
    base = load_configuration(directories.config, roles_file=_roles_file(args.get("roles"), directories.config))
    reset = args.get("reset")
    if reset is not None and any(args.get(key) is not None for key in ("role", "model", "engine", "effort")):
        raise ValueError("switch --reset ROLE cannot be combined with model/effort/engine")
    role = string(reset if reset is not None else args.get("role"), "role")
    if role not in base.roles:
        raise ValueError(f"Unknown role: {role}")
    with settings_lock(directories.state):
        path = directories.state / "overrides.json"
        data = object_value(loads(path.read_text(encoding="utf-8")), "overrides") if path.exists() else {"roles": {}}
        overrides = object_value(data.get("roles"), "overrides.roles")
        effective = effective_roles(base.roles, directories.state / "tuning.json", path)
        if reset is not None:
            overrides.pop(role, None)
            chosen = effective_roles(base.roles, directories.state / "tuning.json")[role]
        else:
            model = string(args.get("model"), "model")
            engine = Engine(string(args["engine"], "engine")) if args.get("engine") is not None else None
            catalog = [candidate for configured in base.roles.values() for candidate in (configured, *configured.replacements())
                       if candidate.model == model and (engine is None or candidate.engine == engine)]
            if not catalog:
                raise ValueError(f"Model/engine is not configured: {model}/{engine.value if engine else 'auto'}")
            engines = {candidate.engine for candidate in catalog}
            if len(engines) != 1:
                raise ValueError("Model has multiple engines; specify --engine")
            candidate = catalog[0]
            effort = Effort(string(args["effort"], "effort")) if args.get("effort") is not None else effective[role].effort
            overrides[role] = {"model": model, "engine": candidate.engine.value, "effort": effort.value,
                               "neutral_cwd": candidate.neutral_cwd,
                               "note": "quota_shared_with_chat" if candidate.engine == Engine.CLAUDE else candidate.note or "manual_switch"}
            chosen = replace(effective[role], model=model, engine=candidate.engine, effort=effort)
        atomic_write(path, data)
        event = Event("switch", EventKind.ROLE_SWITCHED, role=role, model=chosen.model, engine=chosen.engine,
                      note="quota_shared_with_chat" if chosen.engine == Engine.CLAUDE else None)
        EventLog(directories.state).append(event)
        for run in sorted(directories.runs.glob("*/state.json")):
            directory = _run_path(directories.runs, run.parent.name)
            state = load_state(directory)
            if state.status in (Status.PENDING, Status.RUNNING, Status.COMPLETED):
                EventLog(directory).append(replace(event, run_id=state.run_id, stage=state.stage))
    print(f"{role}: {chosen.engine.value}/{chosen.model}/{chosen.effort.value}; from the next stage", file=out)
    return 0


def _report(state: RunState, directory: Path, out: TextIO) -> int:
    print(f"{state.run_id}: {state.status.value}; stage={state.stage.value}; attempt={state.attempt}", file=out)
    if state.status == Status.AWAITING_HUMAN:
        print(f"diff: {directory / 'review/diff.patch'}\nchecks: {directory / 'gates.json'}", file=out)
    if state.status == Status.FAILED and (directory / "error.json").is_file():
        data = object_value(loads((directory / "error.json").read_text(encoding="utf-8")), "error")
        print(f"error: {data.get('error')}", file=out)
        if data.get("exit_code") == 3:
            return 3
    return 1 if state.status in (Status.FAILED, Status.STOPPED) else 0


def main(argv: Sequence[str] | None = None, *, adapters: Mapping[Engine, Adapter] | None = None,
         root: Path | None = None, config_directory: Path | None = None,
         state_directory: Path | None = None, runs_directory: Path | None = None,
         neutral_directory: Path | None = None, home: Path | None = None,
         skill_catalog: Mapping[str, str] | None = None,
         rate_limits: Callable[[], LimitRemaining | None] | None = None,
         doctor_probe: Probe | None = None, now: datetime | None = None,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    output = sys.stdout if out is None else out
    errors = sys.stderr if err is None else err
    for stream, injected in ((output, out), (errors, err)):
        if injected is None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                args = object_value(json_value(vars(parser().parse_args(argv))), "arguments")
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 2
        base = root or ROOT

        def directory(name: str, injected: Path | None) -> Path:
            argument = args.get(f"{name}_dir")
            value = string(argument, name) if argument is not None else os.environ.get(f"REONA_{name.upper()}_DIR")
            return (injected or (Path(value) if value else base / name)).resolve()

        dirs = Directories(directory("config", config_directory), directory("state", state_directory), directory("runs", runs_directory))
        command = string(args.get("command"), "command")
        roles_file = _roles_file(args.get("roles"), dirs.config)
        if command == "doctor":
            diagnostics = diagnose(dirs.config, dirs.state, home=home, neutral_directory=neutral_directory,
                                   probe=doctor_probe, roles_file=roles_file)
            for diagnostic in diagnostics:
                print(diagnostic.render(), file=output)
            return 1 if any(diagnostic.level == "error" for diagnostic in diagnostics) else 0
        if command == "gc":
            print(f"Removed expired failure artifacts: {collect_failures(dirs.runs, now=now)}", file=output)
            print(f"Removed completed run links: {collect_run_links(dirs.runs)}", file=output)
            for warning in usage_size_warnings(dirs.state):
                print(f"warning: {warning}", file=errors)
            return 0
        if command == "status":
            identifier = args.get("run_id")
            if identifier is not None:
                directory_path = _run_path(dirs.runs, identifier)
                return _report(load_state(directory_path), directory_path, output)
            else:
                for path in sorted(dirs.runs.glob("*/state.json")):
                    directory_path = _run_path(dirs.runs, path.parent.name)
                    _report(load_state(directory_path), directory_path, output)
            return 0
        if command == "switch":
            return _switch(args, dirs, output)
        if command == "preflight":
            for line in preflight(load_configuration(dirs.config), dirs.state, dirs.runs,
                                  apply=args.get("apply") is True, summary=args.get("summary") is True, now=now):
                print(line, file=output)
            return 0
        configuration = _configuration(dirs, roles_file)
        if command == "run":
            name = _identifier(args.get("project"), "project")
            task = object_value(loads(Path(string(args.get("task"), "task")).read_text(encoding="utf-8")), "task")
            plan = (object_value(loads(Path(string(args["plan"], "plan")).read_text(encoding="utf-8")), "plan")
                    if args.get("plan") is not None else None)
            validate_artifact("task", task)
            if plan is not None:
                normalize_input_plan(plan)
            identifier = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
            run_directory = _run_path(dirs.runs, identifier)
        else:
            run_directory = _run_path(dirs.runs, args.get("run_id"))
            launch = object_value(loads((run_directory / "launch.json").read_text(encoding="utf-8")), "launch")
            name = string(launch.get("project"), "project")
            task, plan = {}, None
        if name not in configuration.projects:
            raise ValueError(f"Unknown project: {name}")
        project = configuration.projects[name]
        if command == "run" and task.get("profile") != project.profile:
            raise ValueError("Task/project profile mismatch")
        engine_adapters = make_adapters(configuration, supplied=adapters, neutral_directory=neutral_directory)

        def roles() -> Mapping[str, RoleConfig]:
            return _configuration(dirs, roles_file).roles

        def limits() -> LimitRemaining | None:
            return codex_rate_limits(cwd=base, event_log=EventLog(run_directory), run_id=run_directory.name)

        pipeline = Pipeline(configuration, project, run_directory, dirs.state, engine_adapters,
                            allow_legacy_argv_checks=args.get("allow_legacy_argv_checks") is True,
                            skill_catalog=skill_catalog, role_provider=roles, config_directory=dirs.config,
                            rate_limits=rate_limits if rate_limits is not None else limits if adapters is None else None)
        if command == "run":
            atomic_write(run_directory / "launch.json", {"project": name})
            state = pipeline.start(RunInputs(task, plan, args.get("research") is True))
        elif command == "approve":
            pipeline.approve()
            state = pipeline.state
        elif command == "reject":
            reason_arg = args.get("reason")
            reason_text = str(reason_arg) if isinstance(reason_arg, str) else ""
            pipeline.reject(reason_text)
            state = pipeline.state
        else:
            state = pipeline.resume(remove_stale=args.get("remove_stale") is True,
                                    retry_failed=args.get("retry_failed") is True)
        events = [object_value(value, "event") for value in read_lines(run_directory / "events.jsonl")]
        for event in events:
            if event.get("event") == "source_dirty":
                print(f"warning: source repository is dirty; changed files: {event.get('changed_file_count')}; "
                      f"untracked files: {event.get('untracked_file_count')}; "
                      "worktree was built from base_sha only", file=output)
        if any(event.get("note") == "quota_shared_with_chat" for event in events):
            print("Claude fallback: quota is shared with chat", file=output)
        if command in ("approve", "reject"):
            _report(state, run_directory, output)
            if command == "approve":
                print(f"worktree: {pipeline.git.root}\nmerge and commit must be done manually", file=output)
            return 0
        code = _report(state, run_directory, output)
        return pipeline.exit_code or code
    except (StageError, RunLockedError) as exc:
        print(f"stage error: {exc}", file=errors)
        return 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"argument/config error: {exc}", file=errors)
        return 2
