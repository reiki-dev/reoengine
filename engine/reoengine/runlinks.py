"""Engine-owned directory links beside worktrees; never traverse their targets."""
from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from ._data import Json, atomic_write, loads, strings

MANIFEST = "run-links.json"
RESERVED = frozenset({"worktree", "launch.json", "inputs.json", "checkpoints", "state.json",
                      "events.jsonl", "prompts", "gates.json", "review", "review.json",
                      "result.json", "fail", "feedback.json", "task.json", "plan.json",
                      "research.json", "error.json", "usage", "run.lock", "run.lock.guard",
                      MANIFEST, ".git"})


def validate_link_name(name: str) -> None:
    if (not name or ".." in name or name in {"."} or name.endswith((".", " "))
            or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in name)
            or PureWindowsPath(name).is_reserved() or name.casefold() in RESERVED):
        raise ValueError("links: expected a single safe, non-reserved name")


def validate_links(links: Mapping[str, Path]) -> None:
    seen: set[str] = set()
    for name, target in links.items():
        validate_link_name(name)
        if name.casefold() in seen:
            raise ValueError("links: duplicate name")
        seen.add(name.casefold())
        if not target.is_absolute() or not target.is_dir():
            raise ValueError("links: target must be an absolute existing directory")


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def run_link_names(directory: Path) -> tuple[str, ...]:
    manifest = directory / MANIFEST
    if not os.path.lexists(manifest):
        return ()
    if is_link(manifest):
        raise ValueError("Run link manifest must be a regular file")
    names = strings(loads(manifest.read_text(encoding="utf-8")), "run links")
    for name in names:
        validate_link_name(name)
    return names


def check_run_links(directory: Path, links: Mapping[str, Path]) -> None:
    """Check presence and target without traversing dependency contents."""
    try:
        for name, target in links.items():
            link = directory / name
            if not is_link(link) or not link.is_dir() or link.resolve() != target.resolve():
                raise ValueError("links: prepared link missing or changed")
    except OSError:
        raise ValueError("links: prepared link unavailable") from None


def ensure_run_links(directory: Path, links: Mapping[str, Path]) -> None:
    try:
        _ensure_run_links(directory, links)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("links: directory link creation failed") from None


def _ensure_run_links(directory: Path, links: Mapping[str, Path]) -> None:
    validate_links(links)
    if not links:
        return
    directory = directory.resolve()
    names = set(run_link_names(directory)) | set(links)
    for name, target in links.items():
        link = directory / name
        if os.path.lexists(link) and (not is_link(link) or not link.is_dir()
                                     or link.resolve() != target.resolve()):
            raise ValueError("Run link conflicts with an existing path")
    # Record the run link before creation so an interrupted preflight is cleanable.
    recorded: list[Json] = list(sorted(names))
    atomic_write(directory / MANIFEST, recorded)
    for name, target in links.items():
        link = directory / name
        if os.path.lexists(link):
            continue
        if os.name == "nt":
            env = dict(os.environ, REOENGINE_RUN_LINK=str(link), REOENGINE_RUN_TARGET=str(target))
            # The command is constant: no user text is interpolated into cmd syntax.
            # Quoted env expansion preserves spaces/metacharacters; delayed expansion
            # is disabled, and cmd does not recursively expand % inside env values.
            result = subprocess.run(
                'cmd.exe /d /v:off /c mklink /J "%REOENGINE_RUN_LINK%" "%REOENGINE_RUN_TARGET%"',
                cwd=directory, env=env, shell=False, timeout=30,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                raise OSError("Could not create run junction")
        else:
            link.symlink_to(target, target_is_directory=True)
    check_run_links(directory, links)


def remove_run_links(directory: Path) -> int:
    """Remove owned links only; callers hold the run lock during cleanup."""
    if is_link(directory):
        raise ValueError("Run directory must not be a link")
    root = directory.resolve()
    count = 0
    for name in run_link_names(root):
        link = root / name
        # Check the absolute link location, not resolve() (which follows its target).
        if link.parent != root:
            raise ValueError("Run link escapes run directory")
        if not os.path.lexists(link):
            continue
        if not is_link(link):
            raise ValueError("Owned run link was replaced by an ordinary path")
        if link.is_symlink():
            os.unlink(link)
        else:
            # Windows os.rmdir removes the junction itself, including a broken one;
            # it does not recurse into or delete the directory at the junction target.
            os.rmdir(link)
        count += 1
    return count
