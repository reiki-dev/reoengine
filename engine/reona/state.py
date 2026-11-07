"""Versioned checkpoints and explicitly reclaimed PID locks."""
from __future__ import annotations

import ctypes
import errno
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType

from ._data import Json, atomic_write, encode, integer, keys, loads, object_value, string

SCHEMA_VERSION = 1


class Stage(str, Enum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    BRIEF = "brief"
    RESEARCH = "research"
    PLAN = "plan"
    IMPLEMENT = "implement"
    GATES = "gates"
    REVIEW = "review"
    FIX_LOOP = "fix_loop"
    ACCEPT = "accept"
    DONE = "done"


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_HUMAN = "awaiting_human"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class RunState:
    run_id: str
    stage: Stage = Stage.CREATED
    status: Status = Status.PENDING
    attempt: int = 0
    base_sha: str = ""
    head_sha: str = ""
    input_hashes: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, str] = field(default_factory=dict)
    tree_hash: str | None = None
    schema_version: int = SCHEMA_VERSION
    fix_previous_failures: int | None = None
    fix_previous_diff_sha256: str | None = None

    def to_json(self) -> dict[str, Json]:
        return {"schema_version": self.schema_version, "run_id": self.run_id,
                "stage": self.stage.value, "status": self.status.value, "attempt": self.attempt,
                "base_sha": self.base_sha, "head_sha": self.head_sha,
                "input_hashes": dict(self.input_hashes), "output_hashes": dict(self.output_hashes),
                "tree_hash": self.tree_hash,
                "fix_previous_failures": self.fix_previous_failures,
                "fix_previous_diff_sha256": self.fix_previous_diff_sha256}


def _text(value: Json, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected string")
    return value


def _hashes(value: Json) -> dict[str, str]:
    return {key: string(item, key) for key, item in object_value(value, "hashes").items()}


def _parse(data: Json) -> RunState:
    value = object_value(data, "state")
    required = {"schema_version", "run_id", "stage", "status", "attempt", "base_sha", "head_sha",
                "input_hashes", "output_hashes", "tree_hash"}
    keys(value, required | {"fix_previous_failures", "fix_previous_diff_sha256"}, "state")
    if required - value.keys():
        raise ValueError("state: missing checkpoint fields")
    version = integer(value.get("schema_version"), "schema_version", 1)
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported state schema_version {version}")
    return RunState(string(value.get("run_id"), "run_id"), Stage(string(value.get("stage"), "stage")),
                    Status(string(value.get("status"), "status")), integer(value.get("attempt"), "attempt"),
                    _text(value.get("base_sha"), "base_sha"), _text(value.get("head_sha"), "head_sha"),
                    _hashes(value.get("input_hashes")), _hashes(value.get("output_hashes")),
                    string(value["tree_hash"], "tree_hash") if value.get("tree_hash") is not None else None,
                    version,
                    integer(value["fix_previous_failures"], "fix_previous_failures")
                    if value.get("fix_previous_failures") is not None else None,
                    string(value["fix_previous_diff_sha256"], "fix_previous_diff_sha256")
                    if value.get("fix_previous_diff_sha256") is not None else None)


def save_state(run_directory: Path, state: RunState) -> None:
    data = state.to_json()
    _parse(data)
    atomic_write(run_directory / "state.json", data)


def load_state(run_directory: Path) -> RunState:
    return _parse(loads((run_directory / "state.json").read_text(encoding="utf-8")))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a safe liveness probe on Windows.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is treated as alive, never steal a lock on uncertainty.
            return ctypes.get_last_error() != 87
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "linux":
        # A killed process awaiting reaping by its new parent is a zombie, not a live process.
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            if stat[stat.rfind(")") + 1:].split()[0] == "Z":
                return False
        except (OSError, IndexError):
            pass
    return True


class RunLockedError(RuntimeError):
    pass


class RunLock:
    def __init__(self, run_directory: Path, *, remove_stale: bool = False) -> None:
        self.path = run_directory / "run.lock"
        self.remove_stale = remove_stale
        self.token = uuid.uuid4().hex
        self._owned = False

    def acquire(self) -> None:
        if self._owned:
            raise RunLockedError("Lock is already acquired by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A separate OS guard serializes stale-check + unlink + exclusive-create.
        # Its inode stays stable; kernel lock state disappears on process death.
        from ._data import file_lock

        guard = os.open(self.path.with_suffix(".lock.guard"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with file_lock(guard):
                if self.path.exists():
                    try:
                        previous = object_value(loads(self.path.read_text(encoding="utf-8")), "run.lock")
                        lock_handle = integer(previous.get("pid"), "run.lock.pid", 1)
                    except (ValueError, OSError) as exc:
                        raise RunLockedError("Unreadable run.lock; manual inspection required") from exc
                    if pid_alive(lock_handle):
                        raise RunLockedError(f"Run is locked by PID {lock_handle}")
                    if not self.remove_stale:
                        raise RunLockedError(f"Stale run.lock for PID {lock_handle}; explicit remove_stale required")
                    self.path.unlink()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(encode({"pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat(),
                                             "token": self.token}))
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError:
                    self.path.unlink(missing_ok=True)
                    raise
                self._owned = True
        finally:
            os.close(guard)

    def release(self) -> None:
        if not self._owned:
            return
        from ._data import file_lock

        guard = os.open(self.path.with_suffix(".lock.guard"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with file_lock(guard):
                data = object_value(loads(self.path.read_text(encoding="utf-8")), "run.lock")
                if data.get("token") != self.token or data.get("pid") != os.getpid():
                    raise RunLockedError("run.lock ownership changed")
                self.path.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        finally:
            self._owned = False
            os.close(guard)

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.release()
