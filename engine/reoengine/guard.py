"""Detect writes outside the worktree. This is not an OS sandbox.

Source files are never opened: only Git metadata and lstat are used. Other
categories use SHA-256, except prohibited names and links (metadata only).
Snapshots stay in memory; paths and contents must never enter events/errors.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .gitops import Git, secret_path
from .runlinks import run_link_names

Category = Literal["engine_state", "config", "source_repo", "state_dir"]
# Shared by ignored working-tree entries and persistent Git metadata entries.
MAX_IGNORED_ENTRIES = 200_000


def _hash(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while limit is None or limit > 0:
            chunk = stream.read(65536 if limit is None else min(65536, limit))
            if not chunk:
                break
            digest.update(chunk)
            if limit is not None:
                limit -= len(chunk)
    return digest.hexdigest()


def _metadata(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    return f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}"


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _collect(path: Path, root: Path, files: dict[str, str]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    name = path.relative_to(root).as_posix()
    if _linked(info) or secret_path(name) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        # Do not follow junctions/symlinks or open secret/data files.
        files[name] = "metadata:" + _metadata(path)
    elif stat.S_ISDIR(info.st_mode):
        for child in sorted(path.iterdir()):
            _collect(child, root, files)
    else:
        files[name] = "sha256:" + _hash(path)


@dataclass(frozen=True)
class SourceSnapshot:
    head: str
    status_hash: str
    paths: dict[str, str]


def _metadata_tree(path: Path, name: str, paths: dict[str, str], budget: int, *,
                   git_metadata: bool = False) -> int:
    """Collect only lstat metadata, with a shared per-snapshot entry budget."""
    pending = [(path, name)]
    count = 1
    if count > budget:
        raise ValueError("Metadata entry limit exceeded")
    prefix = "metadata:" if git_metadata else b"!!".hex() + ":"
    while pending:
        current, relative = pending.pop()
        try:
            info = current.lstat()
        except FileNotFoundError:
            paths[relative] = prefix + "missing"
            continue
        directory = stat.S_ISDIR(info.st_mode) and not _linked(info)
        # Git creates/removes *.lock inside refs, info, etc. Directory size/mtime
        # would indirectly track those excluded files, so retain only its mode.
        paths[relative] = (prefix + f"directory:{info.st_mode}" if git_metadata and directory
                           else prefix + f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}")
        if not directory:
            continue
        children: list[tuple[Path, str]] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if git_metadata and entry.name.endswith(".lock"):
                        continue
                    count += 1
                    if count > budget:
                        raise ValueError("Metadata entry limit exceeded")
                    children.append((current / entry.name, relative.rstrip("/") + "/" + entry.name))
        except FileNotFoundError:
            # Preserve disappearance as a change, never silently omit an entry.
            paths[relative] = prefix + "missing"
            continue
        pending.extend(sorted(children, key=lambda child: child[1], reverse=True))
    return count


def _git_metadata(git: Git, paths: dict[str, str], budget: int) -> int:
    output = git.command(("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"))
    directories = [Path(os.fsdecode(line)) for line in output.splitlines()]
    if len(directories) != 2 or not all(path.is_absolute() for path in directories):
        raise ValueError("Invalid Git directory metadata")
    personal, common = directories
    shared_names = ("config", "config.worktree", "HEAD", "packed-refs", "info", "hooks", "refs")
    private_names = ("HEAD", "config.worktree", "info", "hooks")
    roots: list[tuple[str, Path, tuple[str, ...]]] = [("common", common, shared_names)]
    if personal != common:
        roots.append(("private", personal, private_names))
    count = 0
    for label, root, names in roots:
        prefix = f"gitdir:{label}/"
        # Do not traverse a Git directory that is itself a link/junction.
        if _linked(root.lstat()):
            count += _metadata_tree(root, prefix, paths, budget - count, git_metadata=True)
            continue
        for name in names:
            count += _metadata_tree(root / name, prefix + name, paths, budget - count,
                                    git_metadata=True)
    return count


def source_snapshot(git: Git) -> SourceSnapshot | None:
    """Read status and persistent Git/ignored metadata without opening source files."""
    try:
        head = git.head()
        status = git.command(("status", "--porcelain=v1", "-z", "--untracked-files=all", *git.status_pathspec))
        # Traditional --ignored with -uall expands ignored directories. Use
        # -unormal for !! roots, then bound and inspect their metadata ourselves.
        ignored_status = git.command(("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored",
                                      *git.status_pathspec))
        paths: dict[str, str] = {}
        excluded_names = git.excluded_source_names()
        ignored_entries = len(excluded_names)
        if ignored_entries > MAX_IGNORED_ENTRIES:
            raise ValueError("Metadata entry limit exceeded")
        for name in excluded_names:
            paths["excluded:" + name] = "metadata:" + _metadata(git.root / name)
        status_digest = hashlib.sha256(status)
        for output, ignored_only in ((status, False), (ignored_status, True)):
            fields = output.split(b"\0")
            index = 0
            while index < len(fields) and fields[index]:
                record = fields[index]
                if len(record) < 4 or record[2:3] != b" ":
                    raise ValueError("Invalid status record")
                code = record[:2]
                names = [record[3:]]
                if b"R" in code or b"C" in code:
                    index += 1
                    names.append(fields[index])
                if not ignored_only or code == b"!!":
                    if ignored_only:
                        status_digest.update(record + b"\0")
                    for raw in names:
                        name = os.fsdecode(raw)
                        if code == b"!!":
                            ignored_entries += _metadata_tree(git.root / name, name, paths,
                                                              MAX_IGNORED_ENTRIES - ignored_entries)
                        else:
                            paths[name] = code.hex() + ":" + _metadata(git.root / name)
                index += 1
        _git_metadata(git, paths, MAX_IGNORED_ENTRIES - ignored_entries)
        return SourceSnapshot(head, status_digest.hexdigest(), paths)
    except (OSError, ValueError, RuntimeError, IndexError):
        return None


@dataclass(frozen=True)
class Snapshot:
    files: dict[Category, dict[str, str]]
    events_size: int | None
    events_hash: str | None
    source: SourceSnapshot


@dataclass(frozen=True)
class Violation:
    category: Category
    changed_path_count: int


class SnapshotError(ValueError):
    def __init__(self, category: Category) -> None:
        super().__init__(f"outside_snapshot_unavailable:{category}")
        self.category = category


def _changed(before: dict[str, str], after: dict[str, str]) -> int:
    return sum(before.get(name) != after.get(name) for name in before.keys() | after.keys())


class OutsideGuard:
    def __init__(self, run: Path, config: Path, state: Path, source: Git) -> None:
        self.run, self.config, self.state, self.source = run, config, state, source

    def snapshot(self) -> Snapshot:
        files: dict[Category, dict[str, str]] = {"engine_state": {}, "config": {}, "state_dir": {}}
        category: Category = "engine_state"
        try:
            for name in ("state.json", "inputs.json", "task.json", "plan.json", "launch.json",
                         "checkpoints", "gates.json", "review", "run-links.json"):
                _collect(self.run / name, self.run, files[category])
            for name in run_link_names(self.run):
                files[category]["run-link:" + name] = _metadata(self.run / name)
            category = "config"
            _collect(self.config, self.config, files[category])
            category = "state_dir"
            for name in ("overrides.json", "tuning.json", "tuning-log.jsonl"):
                _collect(self.state / name, self.state, files[category])
        except (OSError, ValueError):
            raise SnapshotError(category) from None
        events = self.run / "events.jsonl"
        try:
            info = events.lstat()
        except FileNotFoundError:
            size, digest = None, None
        except OSError:
            raise SnapshotError("engine_state") from None
        else:
            if _linked(info) or not stat.S_ISREG(info.st_mode):
                files["engine_state"]["events.jsonl"] = "metadata:" + _metadata(events)
                size, digest = None, None
            else:
                try:
                    size, digest = info.st_size, _hash(events, info.st_size)
                except OSError:
                    raise SnapshotError("engine_state") from None
        source = source_snapshot(self.source)
        if source is None:
            raise SnapshotError("source_repo")
        return Snapshot(files, size, digest, source)

    def compare(self, before: Snapshot, after: Snapshot) -> tuple[Violation, ...]:
        violations: list[Violation] = []
        for category in ("engine_state", "config", "state_dir"):
            count = _changed(before.files[category], after.files[category])
            if category == "engine_state" and before.events_size is not None:
                try:
                    preserved = (after.events_size is not None and after.events_size >= before.events_size
                                 and _hash(self.run / "events.jsonl", before.events_size) == before.events_hash)
                except OSError:
                    preserved = False
                if not preserved:
                    count += int("events.jsonl" not in after.files[category])
            if count:
                violations.append(Violation(category, count))
        if before.source != after.source:
            violations.append(Violation("source_repo", max(1, _changed(before.source.paths, after.source.paths))))
        return tuple(violations)
