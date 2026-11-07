"""Build every configured engine, including engines reachable only by fallback."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .adapters.agy import AgyAdapter
from .adapters.base import Adapter
from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter
from .adapters.api_json import ApiJsonAdapter
from .config import Configuration, Engine
from .paths import neutral_path


def make_adapters(configuration: Configuration, *, supplied: Mapping[Engine, Adapter] | None = None,
                  neutral_directory: Path | None = None) -> dict[Engine, Adapter]:
    engines = {candidate.engine for role in configuration.roles.values()
               for candidate in (role, *role.replacements())}
    result = dict(supplied or {})
    for engine in engines:
        if engine in result:
            continue
        if supplied is not None:
            raise ValueError(f"Missing injected adapter for {engine.value}")
        if engine == Engine.CODEX:
            result[engine] = CodexAdapter(quota=configuration.limits.quota)
        elif engine == Engine.AGY:
            result[engine] = AgyAdapter(quota=configuration.limits.quota)
        elif engine in {Engine.ANTHROPIC, Engine.OPENAI, Engine.GEMINI}:
            result[engine] = ApiJsonAdapter(engine, quota=configuration.limits.quota,
                                            pricing=configuration.pricing)
        else:
            directory = neutral_directory or neutral_path()
            resolved = directory.resolve()
            if any(resolved.is_relative_to(project.repo.resolve()) for project in configuration.projects.values()):
                raise ValueError("Claude neutral directory must be outside every configured project")
            result[engine] = ClaudeAdapter(directory, quota=configuration.limits.quota)
    return result
