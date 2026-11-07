"""Typed JSON boundary and durable, metadata-only file primitives."""
from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TypeAlias

Json: TypeAlias = "None | bool | int | float | str | list[Json] | dict[str, Json]"

_ATOMIC_REPLACE_ATTEMPTS = 20 if os.name == "nt" else 1
_ATOMIC_REPLACE_DELAY = 0.05


def json_value(value: object) -> Json:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Json] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON keys must be strings")
            result[key] = json_value(item)
        return result
    raise ValueError("Expected finite JSON data")


def loads(text: str) -> Json:
    value: object = json.loads(text)
    return json_value(value)


def object_value(value: Json, where: str) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected object")
    return value


def keys(value: dict[str, Json], allowed: set[str], where: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")


def string(value: Json, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected nonempty string")
    return value


def integer(value: Json, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{where}: expected integer >= {minimum}")
    return value


def number(value: Json, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{where}: expected finite nonnegative number")
    return float(value)


def strings(value: Json, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected string array")
    return tuple(string(item, where) for item in value)


def boolean(value: Json, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: expected boolean")
    return value


def encode(value: Json) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt + 1 == _ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(_ATOMIC_REPLACE_DELAY)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(fd: int, timeout: float = 10.0) -> Iterator[None]:
    """OS locks release on process death; lock the first byte, even for empty files."""
    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out acquiring journal lock") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        if written == 0:
            raise OSError("Journal write made no progress")
        data = data[written:]


def _recover_tail(fd: int, path: Path) -> bool:
    end = os.lseek(fd, 0, os.SEEK_END)
    if end == 0:
        return False
    os.lseek(fd, end - 1, os.SEEK_SET)
    if os.read(fd, 1) == b"\n":
        return False
    # Search backwards in bounded chunks, preserving every completed record.
    start = end
    while start:
        position = max(0, start - 8192)
        os.lseek(fd, position, os.SEEK_SET)
        chunk = os.read(fd, start - position)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            start = position + newline + 1
            break
        start = position
    corrupt = os.open(path.with_name(path.name + ".corrupt"),
                      os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        remaining = end - start
        while remaining:
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                raise OSError("Unexpected end of journal during recovery")
            _write_all(corrupt, chunk)
            remaining -= len(chunk)
        # Preserve the bytes durably before removing them from the journal.
        os.fsync(corrupt)
    finally:
        os.close(corrupt)
    os.ftruncate(fd, start)
    os.fsync(fd)
    return True


def append_line(path: Path, value: Json) -> bool:
    """Append a record; return whether an incomplete tail was archived and removed."""
    data = encode(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The same lock guards readers; a record cannot be observed half-written.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with file_lock(fd):
            recovered = _recover_tail(fd, path)
            original_size = os.lseek(fd, 0, os.SEEK_END)
            try:
                _write_all(fd, data)
                os.fsync(fd)
            except BaseException:
                os.ftruncate(fd, original_size)
                os.fsync(fd)
                raise
            return recovered
    finally:
        os.close(fd)


def read_lines(path: Path) -> list[Json]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with file_lock(fd):
            with os.fdopen(os.dup(fd), "rb") as stream:
                return [loads(line.decode("utf-8")) for line in stream
                        if line.endswith(b"\n") and line.strip()]
    finally:
        os.close(fd)
