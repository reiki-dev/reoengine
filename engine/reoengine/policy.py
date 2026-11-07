"""Pure command and path policy helpers."""
from __future__ import annotations

import hashlib
import re

from ._data import Json, encode, strings

def _commands(value: Json) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError("Expected check argv arrays")
    commands = tuple(strings(item, "check argv") for item in value)
    if any(not command or not command[0] or any("\x00" in arg for arg in command) for command in commands):
        raise ValueError("Invalid check argv")
    return commands

def _path_segments(path: str) -> tuple[str, ...]:
    from .fsm import StageError
    parts = tuple(path.split("/"))
    if (not path or "\\" in path or ":" in path or "\x00" in path
            or any(part in ("", ".", "..") for part in parts)):
        raise StageError(f"Invalid allowed_paths pattern: {path!r}")
    return parts

def _within_paths(path: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return path == parent or (parent[-1] == "**" and len(path) > len(parent) - 1
                              and not any(char in part for part in parent[:-1] for char in "*?[")
                              and path[:len(parent) - 1] == parent[:-1])

def _digest(value: Json) -> str:
    return hashlib.sha256(encode(value)).hexdigest()

def _diff_path(value: bytes) -> str:
    """Decode Git's C-quoted paths, including octal UTF-8 bytes."""
    if value.startswith(b'"') and value.endswith(b'"'):
        escapes = {b"a": b"\a", b"b": b"\b", b"t": b"\t", b"n": b"\n",
                   b"v": b"\v", b"f": b"\f", b"r": b"\r", b'"': b'"', b"\\": b"\\"}

        def unescape(match: re.Match[bytes]) -> bytes:
            token = match.group(1)
            return bytes((int(token, 8),)) if token[:1].isdigit() else escapes[token]

        value = re.sub(rb'\\([0-7]{1,3}|[abtnvfr"\\])', unescape, value[1:-1])
    return value.decode("utf-8", errors="replace")

