"""Agent adapters. Constructors accept fake commands; no invocation at import time."""
from __future__ import annotations

from .agy import AgyAdapter
from .base import Adapter, AgentResult, ErrorKind
from .claude import ClaudeAdapter
from .codex import CodexAdapter, codex_rate_limits

__all__ = ["Adapter", "AgentResult", "ErrorKind", "AgyAdapter", "ClaudeAdapter", "CodexAdapter", "codex_rate_limits"]
