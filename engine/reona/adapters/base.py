"""Typed adapter contract and common strict protocol parsing."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .._data import Json, atomic_write, integer, loads, number, object_value
from ..config import QuotaConfig, RoleConfig
from ..procs import ProcessResult, run_process
from ..schemas import validate
from ..usage import TokenUsage


class ErrorKind(str, Enum):
    QUOTA = "quota"
    TIMEOUT = "timeout"
    PROCESS = "process"
    PROTOCOL = "protocol"
    SCHEMA = "schema"


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    stdout_tail: str
    usage: TokenUsage
    duration: float
    final_message_path: Path | None
    stderr_tail: str = ""
    error: ErrorKind | None = None
    note: str | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and self.error is None


class Adapter(Protocol):
    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult: ...


def process_error(result: ProcessResult, quota: QuotaConfig, stderr: str | None = None) -> ErrorKind | None:
    if result.timed_out:
        return ErrorKind.TIMEOUT
    if quota.matches(result.exit_code, result.stderr_tail if stderr is None else stderr):
        return ErrorKind.QUOTA
    if result.exit_code != 0:
        return ErrorKind.PROCESS
    return None


def parse_usage(value: Json, engine: str, root: dict[str, Json] | None = None) -> TokenUsage:
    if value is None:
        return TokenUsage()
    usage = object_value(value, "agent usage")
    def token(key: str) -> int | None:
        item = usage.get(key)
        return None if item is None else integer(item, key)
    def metric(value: Json, name: str) -> float | None:
        return None if value is None else number(value, name)
    cache_key = "cached_input_tokens" if engine == "codex" else "cache_read_tokens" if engine == "agy" else "cache_read_input_tokens"
    reasoning_key = "thinking_tokens" if engine == "agy" else "reasoning_output_tokens"
    metadata = root or {}
    total_cost = metric(metadata.get("total_cost_usd"), "total_cost_usd")
    return TokenUsage(token("input_tokens"), token(cache_key), token("output_tokens"), token(reasoning_key),
                      total_cost, metric(metadata.get("duration_seconds"), "duration_seconds"),
                      "reported" if total_cost is not None else None)


def validate_response(path: Path, schema: Path | None) -> None:
    if schema is not None:
        definition = object_value(loads(schema.read_text(encoding="utf-8")), "response schema")
        validate(loads(path.read_text(encoding="utf-8")), definition)


def invoke_json(command: Sequence[str], role_cfg: RoleConfig, prompt_file: Path, cwd: Path,
                timeout: float, schema: Path | None, output_path: Path,
                quota: QuotaConfig, engine: str) -> AgentResult:
    chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    try:
        process = run_process(command, cwd=cwd, timeout=timeout, stdin_file=prompt_file,
                              on_stdout=chunks.append, on_stderr=stderr_chunks.append)
    except (OSError, ValueError):
        return AgentResult(-1, "", TokenUsage(), 0, None, error=ErrorKind.PROCESS, note=role_cfg.note)
    error = process_error(process, quota, b"".join(stderr_chunks).decode("utf-8", errors="replace"))
    usage = TokenUsage()
    final: Path | None = None
    denied = False
    if error is None:
        try:
            response = object_value(loads(b"".join(chunks).decode("utf-8")), "agent response")
            denied = engine == "agy" and bool(response.get("denied_actions"))
            usage = parse_usage(response.get("usage"), engine, response)
            structured = engine == "agy" and schema is not None
            if (denied or response.get("is_error") is True or response.get("error") is not None
                    or (structured and response.get("status") != "SUCCESS")):
                error = ErrorKind.PROTOCOL
            elif structured and not isinstance(response.get("structured_output"), dict):
                error = ErrorKind.SCHEMA
            else:
                message = response.get("structured_output") if structured else response.get("result", response.get("response"))
                if message is None:
                    raise ValueError("Missing final result/response")
                # A response artifact is intentional output, separate from metadata logs.
                if isinstance(message, str):
                    output_path.write_text(message, encoding="utf-8")
                else:
                    atomic_write(output_path, message)
                final = output_path
                try:
                    validate_response(output_path, schema)
                except ValueError:
                    error = ErrorKind.SCHEMA
        except (ValueError, UnicodeError, OSError):
            error = ErrorKind.PROTOCOL
    return AgentResult(process.exit_code, "" if denied else process.stdout_tail, usage, process.duration, final,
                       "agy denied one or more actions" if denied else process.stderr_tail, error, role_cfg.note)
