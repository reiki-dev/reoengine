"""Platform-specific default paths shared by adapters and diagnostics."""
from __future__ import annotations

import os
from pathlib import Path


def neutral_path() -> Path:
    value = os.environ.get("REOENGINE_NEUTRAL_DIR")
    if value:
        return Path(value).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).resolve() / "reoengine" / "neutral"
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).resolve() / "reoengine" / "neutral"
    return Path.home() / ".local" / "state" / "reoengine" / "neutral"
