"""Per-role semaphores and a cross-process, machine-wide writer lock."""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ._data import file_lock
from .config import Limits


class WriterLockTimeout(RuntimeError):
    pass


class Slots:
    def __init__(self, limits: Limits, state_directory: Path) -> None:
        if limits.slots.get("coder") != 1 or limits.slots.get("reviewer") != 1:
            raise ValueError("coder and reviewer slots must be 1")
        if not 1 <= limits.max_slots <= 8 or any(not 1 <= n <= limits.max_slots for n in limits.slots.values()):
            raise ValueError("Invalid slot ceiling")
        self.directory = state_directory
        self.timeout = limits.timeout_seconds
        self.total = threading.BoundedSemaphore(limits.max_slots)
        self.roles = {name: threading.BoundedSemaphore(count) for name, count in limits.slots.items()}
        self.guard = threading.Lock()
        self.writers: dict[str, threading.BoundedSemaphore] = {}

    @contextmanager
    def acquire(self, role: str, run_id: str, *, writing: bool = False) -> Iterator[None]:
        group = "coder" if writing else role if role in self.roles else "readers"
        with self.guard:
            writer = self.writers.setdefault(run_id, threading.BoundedSemaphore(1))
        with self.roles[group], self.total:
            if not writing:
                yield
                return
            with writer:
                self.directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.directory / "writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    with ExitStack() as stack:
                        try:
                            stack.enter_context(file_lock(fd, timeout=self.timeout))
                        except TimeoutError as exc:
                            raise WriterLockTimeout(f"Timed out waiting for writer.lock after {self.timeout} seconds") from exc
                        yield
                finally:
                    os.close(fd)
