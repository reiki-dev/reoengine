from __future__ import annotations

import subprocess
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from queue import Queue

from .._data import Json, loads, number, object_value
from ..config import Engine, QuotaConfig, RoleConfig
from ..events import Event, EventKind, EventLog, LimitRemaining
from ..procs import run_process
from ..usage import TokenUsage
from .base import AgentResult, ErrorKind, parse_usage, process_error, validate_response


def _inside_git_work_tree(cwd: Path) -> bool:
    try:
        result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, shell=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


class CodexAdapter:
    def __init__(self, command: Sequence[str] = ("codex",), *, quota: QuotaConfig | None = None) -> None:
        self.command = tuple(command)
        self.quota = quota or QuotaConfig()

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        if role_cfg.engine != Engine.CODEX:
            raise ValueError("CodexAdapter requires engine=codex")
        output = prompt_file.resolve().parent / f"response-{uuid.uuid4().hex}.json"
        # TOML literal strings avoid double quotes in Windows batch shim arguments.
        # Single quotes in model_reasoning_effort are required by Windows shims; POSIX accepts this valid TOML too.
        command = [*self.command, "exec", "--model", role_cfg.model, "--json", "--output-last-message", str(output),
                   "-c", f"model_reasoning_effort='{role_cfg.effort.value}'", "-c", "approval_policy='never'",
                   "--sandbox", role_cfg.sandbox.value]
        # Digest reviewers run in a neutral directory outside any Git work tree.
        if not _inside_git_work_tree(cwd):
            command.append("--skip-git-repo-check")
        if schema is not None:
            command.extend(["--output-schema", str(schema.resolve())])
        command.append("-")
        chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        try:
            process = run_process(command, cwd=cwd, timeout=timeout, stdin_file=prompt_file.resolve(),
                                  on_stdout=chunks.append, on_stderr=stderr_chunks.append)
        except (OSError, ValueError):
            return AgentResult(-1, "", TokenUsage(), 0, None, error=ErrorKind.PROCESS, note=role_cfg.note)
        error = process_error(process, self.quota, b"".join(stderr_chunks).decode("utf-8", errors="replace"))
        completed = False
        failed = False
        usage = TokenUsage()
        try:
            for line in b"".join(chunks).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                event = object_value(loads(line), "codex event")
                if not isinstance(event.get("type"), str):
                    raise ValueError("Codex event missing type")
                if event.get("type") == "turn.completed":
                    completed = True
                    usage = parse_usage(event.get("usage"), "codex")
                if event.get("type") in ("turn.failed", "error") or event.get("error") is not None:
                    failed = True
        except (ValueError, UnicodeError):
            failed = True
        if error is None and (not completed or failed):
            error = ErrorKind.PROTOCOL
        final = output if output.is_file() else None
        if error is None:
            if final is None:
                error = ErrorKind.PROTOCOL
            else:
                try:
                    validate_response(final, schema)
                except (ValueError, OSError):
                    error = ErrorKind.SCHEMA
        return AgentResult(process.exit_code, process.stdout_tail, usage, process.duration, final,
                           process.stderr_tail, error, role_cfg.note)


def _window(value: Json) -> tuple[int, float] | None:
    if value is None:
        return None
    data = object_value(value, "rate limit window")
    duration = number(data.get("windowDurationMins"), "windowDurationMins")
    used = number(data.get("usedPercent"), "usedPercent")
    if used > 100 or duration not in (300, 10080):
        raise ValueError("Unexpected rate limit window")
    return int(duration), 100.0 - used


def _rate_result(value: Json) -> LimitRemaining:
    result = object_value(value, "rate limits result")
    data = object_value(result.get("rateLimits"), "rateLimits")
    windows = [_window(data.get("primary")), _window(data.get("secondary"))]
    values = {window[0]: window[1] for window in windows if window is not None}
    if not values:
        raise ValueError("No known rate limit windows")
    return LimitRemaining(values.get(300), values.get(10080))


def codex_rate_limits(*, cwd: Path, event_log: EventLog, run_id: str,
                      command: Sequence[str] = ("codex",), timeout: float = 10.0) -> LimitRemaining | None:
    """JSON-RPC stdio probe. Unknown/unavailable limits produce a metadata-only event."""
    requests: Queue[str | None] = Queue()
    requests.put('{"id":1,"method":"initialize","params":{"clientInfo":{"name":"reona","version":"0.1.0"}}}\n')
    complete = threading.Event()
    buffer = bytearray()
    result: LimitRemaining | None = None
    initialized = False

    def receive(chunk: bytes) -> None:
        nonlocal initialized, result
        buffer.extend(chunk)
        while b"\n" in buffer and not complete.is_set():
            line, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            if not line.strip():
                continue
            try:
                response = object_value(loads(line.decode("utf-8")), "app-server response")
                if response.get("id") == 1:
                    if initialized or response.get("error") is not None or not isinstance(response.get("result"), dict):
                        raise ValueError("Initialization failed")
                    initialized = True
                    requests.put('{"method":"initialized","params":{}}\n')
                    requests.put('{"id":2,"method":"account/rateLimits/read","params":{}}\n')
                elif response.get("id") == 2:
                    if not initialized or response.get("error") is not None:
                        raise ValueError("Rate limits RPC failed")
                    result = _rate_result(response.get("result"))
                    complete.set()
                    requests.put(None)
            except (ValueError, UnicodeError):
                complete.set()
                requests.put(None)

    try:
        process = run_process([*command, "app-server"], cwd=cwd, timeout=timeout, stdin_queue=requests,
                              on_stdout=receive, stop_event=complete)
        if result is not None and not process.timed_out:
            return result
    except (OSError, ValueError, UnicodeError, RuntimeError):
        pass
    event_log.append(Event(run_id=run_id, event=EventKind.RATE_LIMITS_UNAVAILABLE, engine=Engine.CODEX))
    return None
