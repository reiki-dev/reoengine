"""Delete expired failure artifacts, never recurse through project trees."""
from __future__ import annotations

import stat
from datetime import datetime, timezone
from pathlib import Path

from ._data import loads, object_value, string
from .runlinks import MANIFEST, remove_run_links
from .state import RunLock, RunLockedError, Status, load_state


def usage_size_warnings(state_directory: Path, *, limit_bytes: int = 50 * 1024 * 1024) -> list[str]:
    if limit_bytes < 0:
        raise ValueError("usage size limit must be nonnegative")
    candidates = [path for path in sorted(state_directory.glob("usage-????-??.jsonl"))
                  if path.name[6:10].isdigit() and path.name[11:13].isdigit()]
    outcomes = state_directory / "task-outcomes.jsonl"
    if outcomes.exists() or outcomes.is_symlink():
        candidates.append(outcomes)
    warnings: list[str] = []
    mib = 1024 * 1024
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size <= limit_bytes:
            continue
        warnings.append(f"{path.name}: {info.st_size / mib:.6f} MiB exceeds {limit_bytes / mib:g} MiB")
    return warnings


def _link(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def collect_failures(runs_directory: Path, *, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise ValueError("GC time must be timezone-aware")
    count = 0
    if not runs_directory.exists() or _link(runs_directory):
        return 0
    root = runs_directory.resolve()
    for run in runs_directory.iterdir():
        if _link(run) or not run.is_dir():
            continue
        failure = run / "fail"
        if not failure.is_dir() or _link(failure):
            continue
        for path in failure.glob("*.json"):
            if _link(path) or not path.is_file() or not path.resolve().is_relative_to(root / run.name / "fail"):
                continue
            try:
                data = object_value(loads(path.read_text(encoding="utf-8")), "failure")
                expiry = datetime.fromisoformat(string(data.get("expires_at"), "expires_at"))
                if expiry.utcoffset() is not None and expiry <= current:
                    path.unlink()
                    count += 1
            except (OSError, ValueError):
                # Unknown/malformed expiration is never permission to delete.
                continue
    return count


def collect_run_links(runs_directory: Path) -> int:
    """Unlink terminal runs only, without following or enumerating link targets."""
    if not runs_directory.exists() or _link(runs_directory):
        return 0
    count = 0
    for run in runs_directory.iterdir():
        if _link(run) or not run.is_dir() or not (run / MANIFEST).is_file():
            continue
        try:
            with RunLock(run):
                if load_state(run).status in (Status.AWAITING_HUMAN, Status.STOPPED, Status.FAILED):
                    count += remove_run_links(run)
        except (OSError, ValueError, RunLockedError):
            continue
    return count
