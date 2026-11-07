from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import replace
from collections.abc import Sequence
from pathlib import Path

from ..config import Engine, QuotaConfig, RoleConfig
from ..paths import neutral_path
from .base import AgentResult, invoke_json


def _validate_neutral_directory(directory: Path) -> None:
    global_instruction = os.path.normcase((Path.home() / ".claude" / "CLAUDE.md").resolve())
    for ancestor in (directory, *directory.parents):
        for relative in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            instruction = ancestor / relative
            # Global user instructions are explicitly allowed by the fallback policy.
            if os.path.normcase(instruction.resolve()) == global_instruction:
                continue
            if instruction.exists():
                raise ValueError(f"Claude neutral directory inherits project instructions: {instruction}")
        if (ancestor / ".git").exists():
            raise ValueError(f"Claude neutral directory is inside a project: {ancestor}")


class ClaudeAdapter:
    """Only the Opus fallback. Each invocation gets a fresh empty neutral directory."""
    def __init__(self, neutral_directory: Path | None = None, command: Sequence[str] = ("claude",), *,
                 quota: QuotaConfig | None = None) -> None:
        if neutral_directory is None:
            neutral_directory = neutral_path()
        self.neutral_directory = neutral_directory.resolve()
        _validate_neutral_directory(self.neutral_directory)
        self.command = tuple(command)
        self.quota = quota or QuotaConfig()

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        if role_cfg.engine != Engine.CLAUDE or role_cfg.model != "opus" or not role_cfg.neutral_cwd:
            raise ValueError("ClaudeAdapter is only for the neutral Opus fallback")
        role_cfg = replace(role_cfg, note="quota_shared_with_chat")
        _validate_neutral_directory(self.neutral_directory.resolve())
        self.neutral_directory.mkdir(parents=True, exist_ok=True)
        output = prompt_file.resolve().parent / f"response-{uuid.uuid4().hex}.json"
        command = [*self.command, "-p", "--model", "opus", "--tools", "", "--permission-mode", "plan",
                   "--output-format", "json"]
        with tempfile.TemporaryDirectory(prefix="opus-", dir=self.neutral_directory) as neutral:
            _validate_neutral_directory(Path(neutral).resolve())
            return invoke_json(command, role_cfg, prompt_file.resolve(), Path(neutral), timeout, schema,
                               output, self.quota, "claude")
