"""Strict configuration loaders. No loader writes to the configuration directory."""
from __future__ import annotations

import re
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from ._data import (Json, boolean, integer, json_value, keys, loads, number,
                    object_value, string, strings)
from .runlinks import validate_links
from .gitops import validate_worktree_exclusions


_CONFIG_VARIABLE = re.compile(r"\$\{(REOENGINE_ROOT|HOME|ENV:([A-Za-z_][A-Za-z0-9_]*))\}")


def _expand_config_string(value: str, path: Path) -> str:
    """Expand the documented path and argv variables without interpreting other dollar forms."""
    repo_root = Path(__file__).resolve().parents[2]

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        if variable == "REOENGINE_ROOT":
            return str(repo_root)
        if variable == "HOME":
            return str(Path.home())
        name = match.group(2)
        if name not in os.environ:
            raise ValueError(f"{path}: environment variable {name} is not set")
        return os.environ[name]

    return _CONFIG_VARIABLE.sub(replace, value)


class Engine(str, Enum):
    CODEX = "codex"
    AGY = "agy"
    CLAUDE = "claude"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class Effort(str, Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class Sandbox(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True)
class Fallback:
    engine: Engine
    model: str
    neutral_cwd: bool = False
    note: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class RoleConfig:
    engine: Engine
    model: str
    effort: Effort = Effort.HIGH
    sandbox: Sandbox = Sandbox.READ_ONLY
    fallback: tuple[str, ...] = ()
    fallback_chain: tuple[Fallback, ...] = ()
    neutral_cwd: bool = False
    note: str | None = None
    disable_skills: bool = False
    api_key_env: str | None = None
    base_url: str | None = None

    def replacements(self) -> tuple[RoleConfig, ...]:
        result = [replace(self, model=model, engine=_model_engine(model, self.engine),
                          fallback=(), fallback_chain=()) for model in self.fallback]
        result.extend(replace(self, engine=item.engine, model=item.model,
                              neutral_cwd=item.neutral_cwd, note=item.note,
                              api_key_env=item.api_key_env or self.api_key_env,
                              base_url=item.base_url or self.base_url,
                              fallback=(), fallback_chain=()) for item in self.fallback_chain)
        return tuple(result)


def _model_engine(model: str, default: Engine) -> Engine:
    if model.startswith("gpt-"):
        return Engine.CODEX
    if model.startswith(("gemini-", "claude-")):
        return Engine.AGY
    return default


@dataclass(frozen=True)
class QuotaConfig:
    exit_codes: tuple[int, ...] = (1, 3, 29, 429)
    stderr_patterns: tuple[str, ...] = (r"\brate.?limit\b", r"\bquota\b", r"usage limit", r"too many requests",
                                    "QUOTA_EXHAUSTED", "RESOURCE_EXHAUSTED")

    def matches(self, exit_code: int, stderr: str) -> bool:
        return exit_code in self.exit_codes and any(re.search(pattern, stderr, re.IGNORECASE)
                                                   for pattern in self.stderr_patterns)


@dataclass(frozen=True)
class IsolationConfig:
    mode: str = "env"
    wrapper: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    max_slots: int = 8
    slots: dict[str, int] = field(default_factory=lambda: {"coder": 1, "reviewer": 1, "readers": 4, "council": 3})
    timeout_seconds: float = 600.0
    max_fix_rounds: int = 3
    fix_loop_token_budget: int = 400_000
    fix_loop_no_progress_stop: bool = True
    max_prompt_bytes: int = 2_097_152
    limit_threshold_percent: float = 80.0
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    timeouts: dict[str, int] = field(default_factory=dict)
    isolation: IsolationConfig = field(default_factory=IsolationConfig)
    review_full_diff_lines: int = 400
    failure_ttl_days: int = 7


@dataclass(frozen=True)
class Profile:
    skills: tuple[str, ...] = ()
    rules: str = ""
    max_skills: int = 4
    max_skill_tokens: int = 2500
    blocked_paths: tuple[str, ...] = ()
    checks: tuple[tuple[str, ...], ...] = ()
    disable_skills: bool = False


@dataclass(frozen=True)
class Project:
    repo: Path
    profile: str
    checks: tuple[tuple[str, ...], ...] = ()
    allowed_paths: tuple[str, ...] = ()
    offline_env: dict[str, str] = field(default_factory=dict)
    links: dict[str, Path] = field(default_factory=dict)
    exclude_from_worktree: tuple[str, ...] = ()
    acceptance: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    high_risk_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_links(self.links)
        validate_worktree_exclusions(self.exclude_from_worktree)
        object.__setattr__(self, "acceptance", MappingProxyType(dict(self.acceptance)))


@dataclass(frozen=True)
class Pricing:
    input: float
    output: float
    cached_input: float | None = None
    reasoning_output: float | None = None

    @property
    def cached_input_price(self) -> float:
        return self.input if self.cached_input is None else self.cached_input

    @property
    def reasoning_output_price(self) -> float:
        return self.output if self.reasoning_output is None else self.reasoning_output


@dataclass(frozen=True)
class Configuration:
    roles: dict[str, RoleConfig]
    limits: Limits
    profiles: dict[str, Profile]
    projects: dict[str, Project]
    pricing: Mapping[str, Pricing] = field(default_factory=dict)


def _toml(path: Path) -> dict[str, Json]:
    try:
        with path.open("rb") as stream:
            return object_value(json_value(tomllib.load(stream)), str(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _role(data: dict[str, Json], where: str) -> RoleConfig:
    keys(data, {"engine", "model", "effort", "sandbox", "fallback", "fallback_chain",
                "neutral_cwd", "note", "disable_skills", "api_key_env", "base_url"}, where)
    chain = data.get("fallback_chain", [])
    if not isinstance(chain, list):
        raise ValueError(f"{where}.fallback_chain: expected array")
    fallbacks: list[Fallback] = []
    for entry in chain:
        item = object_value(entry, f"{where}.fallback_chain")
        keys(item, {"engine", "model", "neutral_cwd", "note", "api_key_env", "base_url"}, where)
        api_key_env = _optional_string(item, "api_key_env", where)
        base_url = _validated_base_url(_optional_string(item, "base_url", where), where)
        fallback = Fallback(Engine(string(item.get("engine"), where)), string(item.get("model"), where),
                            boolean(item.get("neutral_cwd", False), where),
                            string(item["note"], where) if "note" in item else None,
                            api_key_env, base_url)
        if fallback.engine == Engine.CLAUDE and (fallback.model != "opus" or not fallback.neutral_cwd):
            raise ValueError(f"{where}: claude fallback requires opus and neutral_cwd=true")
        fallbacks.append(fallback)
    api_key_env = _optional_string(data, "api_key_env", where)
    base_url = _validated_base_url(_optional_string(data, "base_url", where), where)
    role = RoleConfig(Engine(string(data.get("engine"), where)), string(data.get("model"), where),
                      Effort(string(data.get("effort", "high"), where)),
                      Sandbox(string(data.get("sandbox", "read-only"), where)),
                      strings(data.get("fallback", []), where), tuple(fallbacks),
                      boolean(data.get("neutral_cwd", False), where),
                      string(data["note"], where) if "note" in data else None,
                      boolean(data.get("disable_skills", False), where), api_key_env, base_url)
    if role.engine == Engine.CLAUDE and (role.model != "opus" or not role.neutral_cwd):
        raise ValueError(f"{where}: claude requires opus and neutral_cwd=true")
    if role.engine in _API_ENGINES and where.rsplit(".", 1)[-1] not in _JSON_ROLES:
        raise ValueError(f"engine {role.engine.value} supports only JSON-answering roles (architect, reviewer, reader, writer)")
    for fallback in role.fallback_chain:
        if fallback.engine in _API_ENGINES and where.rsplit(".", 1)[-1] not in _JSON_ROLES:
            raise ValueError(f"engine {fallback.engine.value} supports only JSON-answering roles (architect, reviewer, reader, writer)")
    return role


_API_ENGINES = {Engine.ANTHROPIC, Engine.OPENAI, Engine.GEMINI}
_JSON_ROLES = {"architect", "reviewer", "reader", "writer"}


def _optional_string(data: dict[str, Json], key: str, where: str) -> str | None:
    if key not in data:
        return None
    value = string(data[key], f"{where}.{key}")
    if key == "api_key_env" and re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) is None:
        raise ValueError(f"{where}.api_key_env: expected environment variable name matching [A-Z_][A-Z0-9_]*")
    return value


def _validated_base_url(value: str | None, where: str) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    local_http = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
    secure_https = parsed.scheme == "https" and bool(parsed.hostname)
    if (not (local_http or secure_https) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError(f"{where}.base_url: expected https://... or http://127.0.0.1[:port] or http://localhost[:port]")
    return value.rstrip("/")


def load_roles(path: Path) -> dict[str, RoleConfig]:
    data = _toml(path)
    keys(data, {"roles"}, str(path))
    roles = object_value(data.get("roles"), f"{path}.roles")
    if not roles:
        raise ValueError(f"{path}: roles must not be empty")
    return {name: _role(object_value(value, name), f"{path}.roles.{name}") for name, value in roles.items()}


def load_pricing(path: Path) -> dict[str, Pricing]:
    if not path.exists():
        return {}
    data = _toml(path)
    result: dict[str, Pricing] = {}
    for model, raw in data.items():
        values = object_value(raw, f"{path}.{model}")
        keys(values, {"input", "output", "cached_input", "reasoning_output"}, f"{path}.{model}")
        if "input" not in values or "output" not in values:
            raise ValueError(f"{path}.{model}: input and output prices are required")
        input_price = number(values["input"], f"{model}.input")
        output_price = number(values["output"], f"{model}.output")
        result[model] = Pricing(input_price, output_price,
                                number(values["cached_input"], f"{model}.cached_input") if "cached_input" in values else input_price,
                                number(values["reasoning_output"], f"{model}.reasoning_output") if "reasoning_output" in values else output_price)
    return result


def _role_data(role: RoleConfig) -> dict[str, Json]:
    return {"engine": role.engine.value, "model": role.model, "effort": role.effort.value,
            "sandbox": role.sandbox.value, "fallback": list(role.fallback),
            "fallback_chain": [{"engine": item.engine.value, "model": item.model,
                                "neutral_cwd": item.neutral_cwd,
                                **({"api_key_env": item.api_key_env} if item.api_key_env is not None else {}),
                                **({"base_url": item.base_url} if item.base_url is not None else {}),
                                **({"note": item.note} if item.note is not None else {})}
                               for item in role.fallback_chain],
            "neutral_cwd": role.neutral_cwd, "disable_skills": role.disable_skills,
            **({"api_key_env": role.api_key_env} if role.api_key_env is not None else {}),
            **({"base_url": role.base_url} if role.base_url is not None else {}),
            **({"note": role.note} if role.note is not None else {})}


def effective_roles(roles: dict[str, RoleConfig], tuning: Path | None = None,
                    overrides: Path | None = None) -> dict[str, RoleConfig]:
    """Overlay shape: {"roles": {"coder": {"model": "..."}}}; lists replace, never merge."""
    result = dict(roles)
    for path in (tuning, overrides):
        if path is None or not path.exists():
            continue
        data = object_value(loads(path.read_text(encoding="utf-8")), str(path))
        keys(data, {"roles", "limits", "skills"} if path == tuning else {"roles"}, str(path))
        for name, value in object_value(data.get("roles"), str(path)).items():
            if name not in result:
                raise ValueError(f"{path}: unknown role {name}")
            patch = object_value(value, name)
            merged = _role_data(result[name])
            merged.update(patch)
            result[name] = _role(merged, f"{path}.roles.{name}")
    return result


def load_limits(path: Path) -> Limits:
    data = _toml(path)
    keys(data, {"max_slots", "slots", "timeout_seconds", "max_fix_rounds", "fix_loop_token_budget",
                "fix_loop_no_progress_stop", "max_prompt_bytes",
                "review_full_diff_lines", "failure_ttl_days", "limit_threshold_percent", "quota", "timeouts", "isolation"}, str(path))
    default = Limits()
    slots_data = object_value(data.get("slots", dict(default.slots)), f"{path}.slots")
    keys(slots_data, {"coder", "reviewer", "readers", "council"}, f"{path}.slots")
    slots = dict(default.slots)
    slots.update({name: integer(value, name, 1) for name, value in slots_data.items()})
    maximum = integer(data.get("max_slots", 8), "max_slots", 1)
    if maximum > 8 or any(value > maximum for value in slots.values()):
        raise ValueError(f"{path}: slots exceed ceiling (at most 8)")
    if slots["coder"] != 1 or slots["reviewer"] != 1 or not 2 <= slots["readers"] <= 4 or slots["council"] != 3:
        raise ValueError(f"{path}: slots require coder=1, reviewer=1, readers=2..4, council=3")
    quota = object_value(data.get("quota", {}), "quota")
    keys(quota, {"exit_codes", "stderr_patterns"}, "quota")
    codes = quota.get("exit_codes", list(default.quota.exit_codes))
    if not isinstance(codes, list):
        raise ValueError("quota.exit_codes: expected array")
    exit_codes = tuple(integer(code, "quota.exit_codes", 1) for code in codes)
    patterns = strings(quota.get("stderr_patterns", list(default.quota.stderr_patterns)), "quota.stderr_patterns")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"quota.stderr_patterns: invalid regex: {exc}") from exc
    threshold = number(data.get("limit_threshold_percent", 80), "limit_threshold_percent")
    timeout = number(data.get("timeout_seconds", 600), "timeout_seconds")
    if threshold > 100 or timeout == 0:
        raise ValueError("limit threshold must be <=100 and timeout must be positive")
    timeouts = {role: integer(value, f"timeouts.{role}", 1)
                for role, value in object_value(data.get("timeouts", {}), "timeouts").items()}
    isolation_data = object_value(data.get("isolation", {}), f"{path}.isolation")
    keys(isolation_data, {"mode", "wrapper"}, f"{path}.isolation")
    isolation_mode = string(isolation_data.get("mode", "env"), f"{path}.isolation.mode")
    if isolation_mode not in {"env", "wrapper"}:
        raise ValueError(f"{path}.isolation.mode: expected 'env' or 'wrapper'")
    isolation_wrapper = strings(isolation_data.get("wrapper", []), f"{path}.isolation.wrapper")
    if any("\x00" in arg for arg in isolation_wrapper):
        raise ValueError(f"{path}.isolation.wrapper: arguments must not contain NUL")
    if isolation_wrapper and not isolation_wrapper[0]:
        raise ValueError(f"{path}.isolation.wrapper: executable must not be empty")
    if isolation_mode == "wrapper" and not isolation_wrapper:
        raise ValueError(f"{path}.isolation.wrapper: required when mode is 'wrapper'")
    return Limits(maximum, slots, timeout, integer(data.get("max_fix_rounds", 3), "max_fix_rounds"),
                  integer(data.get("fix_loop_token_budget", 400_000), "fix_loop_token_budget"),
                  boolean(data.get("fix_loop_no_progress_stop", True), "fix_loop_no_progress_stop"),
                  integer(data.get("max_prompt_bytes", 2_097_152), "max_prompt_bytes", 1),
                  threshold, QuotaConfig(exit_codes, patterns), timeouts,
                  IsolationConfig(isolation_mode, isolation_wrapper),
                  integer(data.get("review_full_diff_lines", 400), "review_full_diff_lines", 1),
                  integer(data.get("failure_ttl_days", 7), "failure_ttl_days", 1))


def _checks(value: Json, where: str, path: Path | None = None) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected argv arrays")
    commands: list[tuple[str, ...]] = []
    for item in value:
        if not isinstance(item, list) or not item:
            raise ValueError(f"{where}: expected nonempty argv array")
        arguments: list[str] = []
        for argument in item:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError(f"{where}: arguments must be strings without NUL")
            arguments.append(_expand_config_string(argument, path) if path is not None else argument)
        if not arguments[0]:
            raise ValueError(f"{where}: executable must not be empty")
        commands.append(tuple(arguments))
    return tuple(commands)


def check_identifier(value: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value) is None:
        raise ValueError(f"acceptance: invalid check ID {value!r}")
    return value


def load_profile(path: Path) -> Profile:
    data = _toml(path)
    keys(data, {"skills", "rules", "max_skills", "max_skill_tokens", "blocked_paths", "checks", "disable_skills"}, str(path))
    maximum = integer(data.get("max_skills", 4), "max_skills")
    if maximum > 8:
        raise ValueError(f"{path}: max_skills exceeds ceiling 8")
    rules = data.get("rules", "")
    if not isinstance(rules, str):
        raise ValueError(f"{path}.rules: expected string")
    return Profile(strings(data.get("skills", []), "skills"), rules, maximum,
                   integer(data.get("max_skill_tokens", 2500), "max_skill_tokens", 1),
                   strings(data.get("blocked_paths", []), "blocked_paths"),
                   _checks(data.get("checks", []), "checks", path),
                   boolean(data.get("disable_skills", False), "disable_skills"))


def load_project(path: Path) -> Project:
    data = _toml(path)
    keys(data, {"repo", "profile", "checks", "allowed_paths", "offline_env", "links",
                "exclude_from_worktree", "acceptance", "high_risk_paths"}, str(path))
    environment = object_value(data.get("offline_env", {}), "offline_env")
    env = {name: string(value, f"offline_env.{name}") for name, value in environment.items()}
    repo = Path(_expand_config_string(string(data.get("repo"), "repo"), path))
    if not repo.is_absolute():
        raise ValueError(f"{path}.repo: expected absolute path")
    links = {name: Path(_expand_config_string(string(value, f"links.{name}"), path))
             for name, value in object_value(data.get("links", {}), "links").items()}
    acceptance = {check_identifier(name): _checks([value], f"acceptance.{name}", path)[0]
                  for name, value in object_value(data.get("acceptance", {}), "acceptance").items()}
    return Project(repo, string(data.get("profile"), "profile"),
                   _checks(data.get("checks", []), "checks", path),
                   strings(data.get("allowed_paths", []), "allowed_paths"), env, links,
                   strings(data.get("exclude_from_worktree", []), "exclude_from_worktree"), acceptance,
                   strings(data.get("high_risk_paths", []), "high_risk_paths"))


def load_configuration(directory: Path, state_directory: Path | None = None,
                       roles_file: Path | None = None) -> Configuration:
    roles = load_roles(roles_file if roles_file is not None else directory / "roles.toml")
    pricing = load_pricing(directory / "pricing.toml")
    limits = load_limits(directory / "limits.toml")
    profiles = {path.stem: load_profile(path) for path in sorted((directory / "profiles").glob("*.toml"))}
    projects = {path.stem: load_project(path) for path in sorted((directory / "projects").glob("*.toml"))}
    for name, project in projects.items():
        if project.profile not in profiles:
            raise ValueError(f"projects.{name}: unknown profile {project.profile}")
    if state_directory is not None:
        roles = effective_roles(roles, state_directory / "tuning.json", state_directory / "overrides.json")
    configuration = Configuration(roles, limits, profiles, projects, pricing)
    return apply_runtime_tuning(configuration, state_directory / "tuning.json") if state_directory is not None else configuration


def apply_runtime_tuning(configuration: Configuration, path: Path) -> Configuration:
    """Only resource counts live outside the existing role overlay API."""
    if not path.exists():
        return configuration
    data = object_value(loads(path.read_text(encoding="utf-8")), str(path))
    keys(data, {"roles", "limits", "skills"}, str(path))
    limits_data = object_value(data.get("limits", {}), "tuning.limits")
    keys(limits_data, {"slots"}, "tuning.limits")
    slot_data = object_value(limits_data.get("slots", {}), "tuning.slots")
    keys(slot_data, {"coder", "reviewer", "readers", "council"}, "tuning.slots")
    slots = dict(configuration.limits.slots)
    slots.update({key: integer(value, key, 1) for key, value in slot_data.items()})
    if slots["coder"] != 1 or slots["reviewer"] != 1 or any(
            value > min(configuration.limits.max_slots, 8) for value in slots.values()):
        raise ValueError("tuning.slots: writer/reviewer must be 1; slot ceiling is 8")
    profiles = dict(configuration.profiles)
    for name, value in object_value(data.get("skills", {}), "tuning.skills").items():
        if name not in profiles:
            raise ValueError(f"tuning.skills: unknown profile {name}")
        count = integer(value, "skill_count")
        profile = profiles[name]
        if count > min(profile.max_skills, 8, len(profile.skills)):
            raise ValueError(f"tuning.skills.{name}: exceeds profile ceiling")
        profiles[name] = replace(profile, skills=profile.skills[:count])
    return replace(configuration, limits=replace(configuration.limits, slots=slots), profiles=profiles)
