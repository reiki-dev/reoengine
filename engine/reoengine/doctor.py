"""Read-only installation diagnostics; probes execute version flags only."""
from __future__ import annotations

import re
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import procs
from ._data import loads, object_value
from .adapters.claude import _validate_neutral_directory
from .config import Engine, Limits, effective_roles, load_limits, load_pricing, load_project, load_roles
from .paths import neutral_path

Probe = Callable[[Sequence[str]], procs.ProcessResult]
Level = Literal["OK", "warning", "error"]


@dataclass(frozen=True)
class Diagnostic:
    level: Level
    name: str
    detail: str

    def render(self) -> str:
        return f"{self.level}: {self.name}: {self.detail}"


def diagnose(config_directory: Path, state_directory: Path, *, home: Path | None = None,
             neutral_directory: Path | None = None, probe: Probe | None = None,
             python_version: tuple[int, int, int] | None = None,
             roles_file: Path | None = None) -> list[Diagnostic]:
    checks: list[Diagnostic] = []
    version = python_version or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    checks.append(Diagnostic("OK" if version >= (3, 11, 0) else "error", "python", ".".join(map(str, version))))

    def default_probe(argv: Sequence[str]) -> procs.ProcessResult:
        return procs.run_process(argv, cwd=Path.cwd(), timeout=5)

    runner = probe or default_probe
    for name in ("git", "codex", "agy", "claude"):
        try:
            result = runner((name, "--version"))
            ok = result.exit_code == 0 and not result.timed_out
            level: Level = "OK" if ok else "error" if name == "git" else "warning"
            version_text = " ".join(result.stdout_tail.splitlines())[:160] if ok else "version probe failed"
            checks.append(Diagnostic(level, name, version_text or "version unavailable"))
        except (OSError, ValueError, RuntimeError):
            checks.append(Diagnostic("error" if name == "git" else "warning", name, "binary unavailable"))
    checks.append(Diagnostic("OK" if config_directory.is_dir() else "error", "config", str(config_directory)))
    limits_path = config_directory / "limits.toml"
    try:
        limits = load_limits(limits_path) if limits_path.exists() else Limits()
        if limits.isolation.mode == "env":
            checks.append(Diagnostic("warning", "isolation",
                                     "checks run with environment filtering only; see docs/ISOLATION.md"))
        else:
            executable = limits.isolation.wrapper[0]
            try:
                procs.resolve_executable(executable)
            except (OSError, ValueError):
                checks.append(Diagnostic("error", "isolation",
                                         f"wrapper executable not found: {executable}"))
    except (OSError, ValueError):
        pass
    try:
        roles = load_roles(roles_file if roles_file is not None else config_directory / "roles.toml")
        defaults = {Engine.ANTHROPIC: "ANTHROPIC_API_KEY", Engine.OPENAI: "OPENAI_API_KEY",
                    Engine.GEMINI: "GEMINI_API_KEY"}
        roles = effective_roles(roles, state_directory / "tuning.json", state_directory / "overrides.json")
        candidates = [candidate for role in roles.values() for candidate in (role, *role.replacements())]
        pricing = load_pricing(config_directory / "pricing.toml")
        missing_prices = sorted({candidate.model for candidate in candidates
                                 if candidate.engine in {Engine.GEMINI, Engine.ANTHROPIC, Engine.OPENAI}
                                 and candidate.model not in pricing})
        checks.extend(Diagnostic("warning", "pricing",
                                f"no price for {model}; cost will be unknown")
                      for model in missing_prices)
        for engine in sorted({role.engine for role in candidates} & defaults.keys(), key=lambda item: item.value):
            env_name = next((role.api_key_env for role in candidates
                             if role.engine == engine and role.api_key_env), defaults[engine])
            present = bool(os.environ.get(env_name))
            detail = f"key present ({env_name})" if present else f"{env_name} not set"
            checks.append(Diagnostic("OK" if present else "warning", engine.value, detail))
    except (OSError, ValueError):
        pass
    project_directory = config_directory / "projects"
    if project_directory.is_dir():
        project_files = sorted(project_directory.glob("*.toml"))
        if not project_files:
            checks.append(Diagnostic("OK", "project configuration", "valid"))
        for project_file in project_files:
            try:
                load_project(project_file)
                checks.append(Diagnostic("OK", "project configuration", f"{project_file.name}: valid"))
            except (OSError, ValueError) as exc:
                # An unset ${ENV:...} variable only disables that project; it is not a broken install.
                severity: Level = "warning" if "is not set" in str(exc) else "error"
                checks.append(Diagnostic(severity, "project configuration", str(exc)))
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=state_directory) as stream:
            stream.write(b"reoengine-write-probe")
            stream.flush()
        checks.append(Diagnostic("OK", "state", "writable"))
    except OSError:
        checks.append(Diagnostic("error", "state", "not writable"))
    try:
        _validate_neutral_directory((neutral_directory or neutral_path()).resolve())
        checks.append(Diagnostic("OK", "neutral", "no project instructions or git in parent directories"))
    except (OSError, ValueError) as exc:
        checks.append(Diagnostic("error", "neutral", str(exc)))
    settings = (home or Path.home()) / ".claude" / "settings.json"
    try:
        data = object_value(loads(settings.read_text(encoding="utf-8")), "settings")
        permissions = object_value(data.get("permissions", {}), "permissions")
        rules = permissions.get("allow", [])
        if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
            raise ValueError("Invalid allow rules")
        for name, pattern in (("agy", r"\bagy(?:\.exe)?(?=[:\s*)])"),
                              ("codex exec", r"\bcodex(?:\.cmd)?\s+exec(?=[:\s*)])")):
            allowed = any(isinstance(rule, str) and re.search(pattern, rule) is not None for rule in rules)
            checks.append(Diagnostic("OK" if allowed else "warning", f"permission {name}",
                                     "allow rule found" if allowed else "allow rule not found"))
    except (OSError, ValueError):
        checks.append(Diagnostic("warning", "permissions", "settings.json is unavailable or invalid"))
    return checks
