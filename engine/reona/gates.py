"""Deterministic gates. Environment filtering provides no network or filesystem isolation."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import unicodedata
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from ._data import Json, atomic_write
from .gitops import (CANARY_MARKER, CANARY_PATH, CANARY_TEXT, SECRET_PATTERNS, Diff, Git, SafetyError,
                     ensure_safe_tree, normalized_path, relative_path, secret_path)
from .procs import ProcessResult, run_process
from .schemas import validate_artifact

BLOCKED_PATHS = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "REONA.md", "PERSONA.md",
                 "config/**", ".github/**", ".gitlab/**", ".gitlab-ci*", "azure-pipelines*",
                 "Jenkinsfile", ".circleci/**", ".claude/**", ".git/**", CANARY_PATH)
ENV_ALLOW = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                       "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS"})
_SECRET_WORDS = ("password", "passwd", "secret", "api_key", "api-key", "apikey",
                 "authorization", "bearer", "private key", "-----begin", "credential", "cookie")
_URI_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^@\s/]+@", re.IGNORECASE)
_KNOWN_OUTPUT_TOKENS = re.compile(
    r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|(?:ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]+"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]+"
    r"|AIza[A-Za-z0-9_-]{35}"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*\Z)", re.DOTALL
)
_TOKEN_ASSIGNMENT = re.compile(r"\w*token\w*[\"']?\s*[:=]\s*\S", re.IGNORECASE)
# Avoid matching fragments of file paths containing dots or Windows separators.
_OUTPUT_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=.\\-])[A-Za-z0-9+/_=-]{16,}(?![A-Za-z0-9+/_=.\\-])")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ASSERTION_FAILURE = re.compile(
    r"\b(?:AssertionError|AssertionFailedError|ERR_ASSERTION)\b"
    r"|FAILED\s*\(failures=[1-9]\d*"
    r"|^FAIL: \S+|^E\s+assert\b|^FAILED\s+\S+\s+-\s+(?:assert\b|Failed:)", re.MULTILINE
)
_INFRA_ERROR = re.compile(
    r"\b(?:ImportError|ModuleNotFoundError|SyntaxError|IndentationError|TabError|PermissionError"
    r"|FileNotFoundError|OSError|TypeError|NameError|ReferenceError|ERR_MODULE_NOT_FOUND)\b"
    r"|Cannot find (?:module|package)|not recognized|command not found|No such file"
    r"|errors=[1-9]\d*|^ERROR: |\bERROR collecting\b|\b[1-9]\d* errors?\b",
    re.IGNORECASE | re.MULTILINE
)


def _check_kind(exit_code: int, stdout: str, stderr: str, *, timed_out: bool = False
                ) -> Literal["pass", "assertion_failed", "infra_error", "timeout", "unknown"]:
    output = _ANSI_ESCAPE.sub("", stdout) + "\n" + _ANSI_ESCAPE.sub("", stderr)
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "pass"
    if exit_code < 0 or _INFRA_ERROR.search(output):
        return "infra_error"
    if _ASSERTION_FAILURE.search(output):
        return "assertion_failed"
    return "unknown"


def bounded_tail(text: str, maximum: int = 7168) -> str:
    return text.encode("utf-8")[-maximum:].decode("utf-8", errors="ignore") if maximum else ""


def sanitize_output(text: str) -> str:
    """Redact sensitive check output before it enters retry feedback."""
    # Normalize controls first so they cannot split a sensitive word or value.
    text = "".join(char for char in text if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"})
    text = _PRIVATE_KEY_BLOCK.sub("[redacted]", text)
    text = _URI_USERINFO.sub(r"\1[redacted]@", text)
    text = _KNOWN_OUTPUT_TOKENS.sub("[redacted]", text)

    def redact(match: re.Match[str]) -> str:
        value = match.group()
        return "[redacted]" if re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value) else value

    lines: list[str] = []
    for line in text.split("\n"):
        lowered = line.casefold()
        if (any(word in lowered for word in _SECRET_WORDS)
                or _TOKEN_ASSIGNMENT.search(line)
                or any(fnmatch.fnmatchcase(lowered, f"*{pattern}*") for pattern in SECRET_PATTERNS)):
            lines.append("[line redacted]")
        else:
            lines.append(_OUTPUT_TOKEN.sub(redact, line))
    cleaned = "\n".join(lines)
    return cleaned if cleaned.strip() else ""


def build_restricted_env(overrides: Mapping[str, str] | None = None,
                         source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Filter environment variables and stub proxies; direct sockets remain unblocked.

    This provides neither network nor filesystem isolation.
    """
    original = os.environ if source is None else source
    env = {name: value for name, value in original.items() if name.upper() in ENV_ALLOW}
    for name, value in (overrides or {}).items():
        if name.upper().startswith(("PYTHON", "GIT_", "LD_", "DYLD_")) or name.upper() in {
                "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR"}:
            raise ValueError("Unsafe restricted environment override")
        env[name] = value
    for name in set(original) | set(env):
        upper = name.upper()
        if upper.endswith("_HOST"):
            env[name] = "127.0.0.1:9"
        elif upper.endswith("_BASE_URL") or upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
            env[name] = "http://127.0.0.1:9"
    env.update({"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def offline_environment(overrides: Mapping[str, str] | None = None,
                        source: Mapping[str, str] | None = None) -> dict[str, str]:
    warnings.warn("Use build_restricted_env; environment filtering does not isolate network or filesystem access",
                  DeprecationWarning, stacklevel=2)
    return build_restricted_env(overrides, source)


def _matches(name: str, pattern: str, *, basename: bool = False) -> bool:
    """Match normalized path components, case-insensitively.

    ``*``, ``?`` and character classes match within one component; only a
    whole ``**`` component recurses (zero or more directories). Allowed
    literals match only themselves; ``dir/`` and ``dir/**`` include descendants.
    Blocked literals also match descendants; slashless blocked patterns retain
    basename matching at every depth.
    """
    name = relative_path(name).casefold()
    pattern = pattern.replace("\\", "/").casefold()
    directory = pattern.endswith("/")
    pattern = relative_path(pattern.rstrip("/"))
    if basename and "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in name.split("/"))
    if not any(char in pattern for char in "*?["):
        return (name == pattern and (basename or not directory)) or (
            (directory or basename) and name.startswith(pattern + "/"))
    components = tuple(pattern.split("/")) + (("**",) if directory else ())
    parts = name.split("/")

    @lru_cache(maxsize=None)
    def match(part: int, component: int) -> bool:
        if component == len(components):
            return part == len(parts)
        if components[component] == "**":
            return (match(part, component + 1)
                    or (part < len(parts) and match(part + 1, component)))
        return (part < len(parts) and fnmatch.fnmatchcase(parts[part], components[component])
                and match(part + 1, component + 1))

    return match(0, 0)


def validate_paths(root: Path, paths: Sequence[str], allowed: Sequence[str],
                   blocked: Sequence[str] = ()) -> None:
    ensure_safe_tree(root)
    for pattern in (*allowed, *blocked):
        relative_path(pattern.replace("\\", "/").rstrip("/"))
    for name in paths:
        name = relative_path(name)
        if secret_path(name) or any(_matches(name, pattern, basename=True) for pattern in (*BLOCKED_PATHS, *blocked)):
            raise SafetyError("Changed path is blocked")
        if not any(_matches(name, pattern) for pattern in allowed):
            raise SafetyError("Changed path is outside allowed_paths")
        if not (root / name).resolve().is_relative_to(root.resolve()):
            raise SafetyError("Changed path escapes worktree")


HIGH_RISK_PATHS = ("**/adapters/**", "**/gates.py", "**/guard.py", "**/gitops.py", "**/procs.py",
                   "**/fsm.py", "**/config.py", "**/schemas/**", "**/config/**", "**/*.toml",
                   "**/.github/**", "**/auth*", "**/auth*/**", "**/security*", "**/security*/**",
                   "**/*secret*", "**/*secret*/**", "**/*token*", "**/*token*/**",
                   "**/package.json", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
                   "**/poetry.lock", "**/uv.lock", "**/migrations/**", "**/*.sql",
                   "**/Dockerfile*", "**/*compose*.yml", "**/*compose*.yaml", "**/.gitlab-ci.yml",
                   "**/Jenkinsfile*", "**/.circleci/**", "**/bitbucket-pipelines.yml", "**/*.lock",
                   "**/go.mod", "**/go.sum", "**/requirements*.txt", "**/Pipfile", "**/setup.py",
                   "**/setup.cfg", "**/*.sh", "**/*.ps1", "**/Makefile", "**/.env", "**/*.pem",
                   "**/*.key", "**/.gitmodules", "**/.gitattributes")


def risk_tier(changed_paths: Sequence[str], *, high_risk_paths: Sequence[str] = (),
              tdd_gate: bool = True, diff_patch: bytes = b"",
              review_full_diff_lines: int = 400) -> Literal["normal", "high"]:
    # Diff.paths includes deleted names and both sides of renames.
    if not tdd_gate or any(_matches(normalized_path(path), normalized_path(pattern))
                           for path in changed_paths for pattern in (*HIGH_RISK_PATHS, *high_risk_paths)):
        return "high"
    changed_lines = sum(1 for line in diff_patch.decode("utf-8", errors="replace").splitlines()
                        if (line.startswith("+") and not line.startswith("+++ "))
                        or (line.startswith("-") and not line.startswith("--- ")))
    if changed_lines > review_full_diff_lines:
        return "high"
    return "normal"


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    exit_code: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    outcome: Literal["pass", "assertion_failed", "infra_error", "timeout", "unknown"] = "unknown"

    def to_json(self) -> dict[str, Json]:
        stdout = bounded_tail(self.stdout_tail)
        stderr = bounded_tail(self.stderr_tail, 7168 - len(stdout.encode("utf-8")))
        return {"name": self.name, "passed": self.passed, "exit_code": self.exit_code,
                "stdout_tail": stdout, "stderr_tail": stderr, "duration_ms": self.duration_ms,
                "outcome": self.outcome}


@dataclass(frozen=True)
class GateReport:
    checks: tuple[GateCheck, ...]
    isolation: Literal["env-only", "wrapper"] = "env-only"

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_json(self) -> dict[str, Json]:
        data: dict[str, Json] = {"passed": self.passed, "checks": [check.to_json() for check in self.checks],
                                 "isolation": self.isolation}
        validate_artifact("gates", data)
        return data

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    @property
    def feedback(self) -> list[Json]:
        return [{"check": check.name, "exit_code": check.exit_code,
                 "output_tail": bounded_tail(sanitize_output(check.stderr_tail + "\n" + check.stdout_tail), 1536)}
                for check in self.checks if not check.passed]


@dataclass(frozen=True)
class GateRequest:
    base_sha: str
    allowed_paths: tuple[str, ...]
    checks: tuple[tuple[str, ...], ...]
    blocked_paths: tuple[str, ...] = ()
    tdd_gate: bool = True
    tdd_gate_reason: str = ""
    test_paths: tuple[str, ...] = ()
    target_checks: tuple[tuple[str, ...], ...] = ()
    timeout: float = 600
    offline_env: Mapping[str, str] | None = None
    canary_hash: str = hashlib.sha256(CANARY_TEXT.encode()).hexdigest()
    isolation_mode: str = "env"
    isolation_wrapper: tuple[str, ...] = ()


def plant_canary(git: Git) -> str:
    path = git.root / CANARY_PATH
    if path.exists() or path.is_symlink():
        raise SafetyError("Reserved canary path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CANARY_TEXT.encode())
    return hashlib.sha256(CANARY_TEXT.encode()).hexdigest()


def _execute(name: str, argv: Sequence[str], cwd: Path, request: GateRequest) -> GateCheck:
    if not argv or not argv[0] or any("\x00" in arg for arg in argv):
        return GateCheck(name, False, -1, stderr_tail="Invalid check argv", outcome="infra_error")
    try:
        command = tuple(argv)
        if request.isolation_mode == "wrapper":
            wrapper: list[str] = []
            for token in request.isolation_wrapper:
                if token == "{worktree}":
                    wrapper.append(str(cwd))
                elif re.fullmatch(r"\{link:[A-Za-z0-9_.-]+\}", token):
                    wrapper.append(str(cwd / token[6:-1]))
                elif "{" in token or "}" in token:
                    raise ValueError("Unknown isolation wrapper placeholder")
                else:
                    wrapper.append(token)
            if not wrapper or not wrapper[0]:
                raise ValueError("Invalid isolation wrapper")
            command = (*wrapper, *command)
        result: ProcessResult = run_process(command, cwd=cwd, timeout=request.timeout,
                                            env=build_restricted_env(request.offline_env), tail_bytes=7168)
        outcome = _check_kind(result.exit_code, result.stdout_tail, result.stderr_tail,
                              timed_out=result.timed_out)
        return GateCheck(name, outcome == "pass",
                         result.exit_code if not result.timed_out else -1,
                         result.stdout_tail, result.stderr_tail, int(result.duration * 1000), outcome)
    except (OSError, ValueError) as exc:
        return GateCheck(name, False, -1, stderr_tail=type(exc).__name__, outcome="infra_error")


def _test_file(name: str) -> bool:
    path = Path(name)
    return (any(part.casefold() in {"test", "tests", "__tests__"} for part in path.parts[:-1])
            or path.name.startswith("test_") or path.stem.endswith("_test")
            or ".test." in path.name or ".spec." in path.name)


def _tdd(git: Git, diff: Diff, request: GateRequest) -> list[GateCheck]:
    tests = request.test_paths or tuple(name for name in diff.paths if _test_file(name))
    if not tests:
        return [GateCheck("tdd", False, 1, stderr_tail="No changed test files", outcome="unknown")]
    validate_paths(git.root, tests, request.allowed_paths, request.blocked_paths)
    patch = git.diff(request.base_sha, tests).patch
    commands = request.target_checks or request.checks
    if not commands or not patch:
        return [GateCheck("tdd", False, 1, stderr_tail="Missing target checks or test diff", outcome="unknown")]
    results: list[GateCheck] = []
    with git.temporary_worktree(request.base_sha) as base:
        # Applying only the test diff preserves unchanged tests and deletions.
        base.command(("apply", "--binary", "--whitespace=nowarn", "-"), stdin=patch)
        for index, command in enumerate(commands):
            red = _execute(f"tdd_red:{index}", command, base.root, request)
            outcome = _check_kind(red.exit_code, red.stdout_tail, red.stderr_tail,
                                  timed_out=red.outcome == "timeout")
            results.append(GateCheck(red.name, outcome == "assertion_failed", red.exit_code,
                                     red.stdout_tail, red.stderr_tail, red.duration_ms, outcome))
            results.append(_execute(f"tdd_green:{index}", command, git.root, request))
    return results


def run_gates(git: Git, request: GateRequest, output: Path, *,
              on_tdd_disabled: Callable[[str], None] | None = None,
              diff_output: Path | None = None) -> GateReport:
    checks: list[GateCheck] = []
    def finish() -> GateReport:
        report = GateReport(tuple(checks), "wrapper" if request.isolation_mode == "wrapper" else "env-only")
        atomic_write(output, report.to_json())
        return report
    try:
        diff = git.diff(request.base_sha)
        if diff_output is not None:
            diff_output.parent.mkdir(parents=True, exist_ok=True)
            diff_output.write_bytes(diff.patch)
        checks.append(GateCheck("diff", True, outcome="pass"))
        validate_paths(git.root, diff.paths, request.allowed_paths, request.blocked_paths)
        checks.append(GateCheck("paths", True, outcome="pass"))
    except (OSError, ValueError) as exc:
        checks.append(GateCheck("paths", False, 1, stderr_tail=str(exc), outcome="infra_error"))
        return finish()
    checks.append(GateCheck("nonempty_diff", bool(diff.patch), 0 if diff.patch else 1,
                            outcome="pass" if diff.patch else "unknown"))
    canary = git.root / CANARY_PATH
    canary_ok = (canary.is_file() and not canary.is_symlink()
                 and hashlib.sha256(canary.read_bytes()).hexdigest() == request.canary_hash
                 and CANARY_MARKER.encode() not in diff.patch)
    checks.append(GateCheck("canary", canary_ok, 0 if canary_ok else 1,
                            outcome="pass" if canary_ok else "infra_error"))
    if not all(check.passed for check in checks):
        return finish()
    if not request.checks:
        checks.append(GateCheck("project_checks", False, 1, stderr_tail="No project checks", outcome="unknown"))
    for index, command in enumerate(request.checks):
        checks.append(_execute(f"project:{index}", command, git.root, request))
    if request.tdd_gate:
        try:
            checks.extend(_tdd(git, diff, request))
        except (OSError, ValueError) as exc:
            checks.append(GateCheck("tdd", False, 1, stderr_tail=str(exc), outcome="infra_error"))
    else:
        reason = request.tdd_gate_reason.strip()
        checks.append(GateCheck("tdd_disabled", bool(reason), 0 if reason else 1,
                                stderr_tail="" if reason else "tdd_gate_reason required",
                                outcome="pass" if reason else "unknown"))
        if reason and on_tdd_disabled is not None:
            on_tdd_disabled(reason)
    # Checks may create artifacts. Validate their paths as well, so a check cannot
    # add an out-of-scope file after the initial paths gate.
    try:
        final_diff = git.diff(request.base_sha)
        if diff_output is not None:
            diff_output.write_bytes(final_diff.patch)
        validate_paths(git.root, final_diff.paths, request.allowed_paths, request.blocked_paths)
        if (not canary.is_file() or canary.is_symlink()
                or hashlib.sha256(canary.read_bytes()).hexdigest() != request.canary_hash
                or CANARY_MARKER.encode() in final_diff.patch or not final_diff.patch):
            raise SafetyError("Canary or diff changed during checks")
        checks.append(GateCheck("post_checks_paths", True, outcome="pass"))
    except (OSError, ValueError) as exc:
        checks.append(GateCheck("post_checks_paths", False, 1, stderr_tail=str(exc), outcome="infra_error"))
    return finish()
