from __future__ import annotations

import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path

from ..config import Engine, QuotaConfig, RoleConfig, Sandbox
from ..procs import run_process
from .base import AgentResult, invoke_json


class AgyAdapter:
    def __init__(self, command: Sequence[str] | None = None, *, quota: QuotaConfig | None = None,
                 capabilities: Sequence[str] | None = None) -> None:
        self.command = tuple(command) if command is not None else ("agy.exe" if os.name == "nt" else "agy",)
        self.quota = quota or QuotaConfig()
        self._capabilities = {} if capabilities is None else {self.command: frozenset(capabilities)}

    def _check_capabilities(self, cwd: Path) -> None:
        if self.command not in self._capabilities:
            chunks: list[bytes] = []
            try:
                result = run_process([*self.command, "--help"], cwd=cwd, timeout=5,
                                     on_stdout=chunks.append, on_stderr=chunks.append)
            except (OSError, ValueError):
                raise RuntimeError("Could not verify agy sandbox capabilities") from None
            if result.timed_out or result.exit_code != 0:
                raise RuntimeError("Could not verify agy sandbox capabilities")
            help_text = b"".join(chunks).decode("utf-8", errors="replace")
            self._capabilities[self.command] = frozenset(re.findall(r"(?<![\w-])--[\w-]+", help_text))
        if not {"--sandbox", "--mode"}.issubset(self._capabilities[self.command]):
            raise RuntimeError("agy requires --sandbox and --mode support")

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        if role_cfg.engine != Engine.AGY:
            raise ValueError("AgyAdapter requires engine=agy")
        if role_cfg.sandbox is Sandbox.READ_ONLY:
            mode = "plan"
        elif role_cfg.sandbox is Sandbox.WORKSPACE_WRITE:
            mode = "accept-edits"
        else:
            raise ValueError("Unsupported agy sandbox policy")
        self._check_capabilities(cwd)
        output = prompt_file.resolve().parent / f"response-{uuid.uuid4().hex}.json"
        command = [*self.command, "--sandbox", "--mode", mode, "--output-format", "json",
                   "--model", role_cfg.model, "--effort", role_cfg.effort.value]
        if schema is not None:
            command.extend(["--json-schema", str(schema.resolve())])
        if role_cfg.disable_skills:
            command.append("--disable-slash-commands")
        return invoke_json(command, role_cfg, prompt_file.resolve(), cwd, timeout, schema, output,
                           self.quota, "agy")
