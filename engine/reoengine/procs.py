"""Process execution: stdin only for prompts, bounded tails, whole-tree timeout."""
from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import BinaryIO


class _BasicLimit(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", ctypes.c_ulong), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", ctypes.c_ulong),
                ("affinity", ctypes.c_size_t), ("priority_class", ctypes.c_ulong),
                ("scheduling_class", ctypes.c_ulong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", _BasicLimit), ("io", _IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t)]


class _WindowsJob:
    """Attach while suspended: no child can escape before assignment to the job."""
    def __init__(self, pid: int) -> None:
        if sys.platform != "win32":
            raise OSError("Windows job objects are only available on Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.TerminateJobObject.restype = ctypes.c_int
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle: int | None = kernel.CreateJobObjectW(None, None)
        if handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        try:
            limits = _ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle: int | None = kernel.OpenProcess(0x100 | 0x1 | 0x800, False, pid)
            if process_handle is None:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                native = ctypes.WinDLL("ntdll")
                native.NtResumeProcess.argtypes = [ctypes.c_void_p]
                native.NtResumeProcess.restype = ctypes.c_long
                if native.NtResumeProcess(process_handle) != 0:
                    raise OSError("Could not resume job process")
            finally:
                kernel.CloseHandle(process_handle)
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    def terminate(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows job objects are only available on Windows")
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows job objects are only available on Windows")
        self.kernel.CloseHandle(self.handle)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration: float
    timed_out: bool = False
    kill_tree_incomplete: bool = False


def resolve_executable(executable: str, env: Mapping[str, str] | None = None) -> str:
    environment = os.environ if env is None else env
    path = Path(executable)
    extensions = environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if os.name == "nt" else []
    if path.is_absolute() or os.path.dirname(executable):
        for candidate in [path, *(Path(str(path) + ext) for ext in extensions if not path.suffix)]:
            if candidate.is_file():
                return str(candidate.resolve())
        raise FileNotFoundError(f"Executable not found: {executable}")
    if os.name == "nt":
        # shutil.which reads the parent's PATHEXT, so search the supplied environment
        # explicitly. Never silently search the current directory before PATH.
        names = [executable] if path.suffix else [executable + ext for ext in extensions if ext]
        for directory in environment.get("PATH", "").split(os.pathsep):
            if directory:
                for name in names:
                    candidate = Path(directory.strip('"')) / name
                    if candidate.is_file():
                        return str(candidate.resolve())
    else:
        found = shutil.which(executable, path=environment.get("PATH", ""))
        if found is not None:
            return str(Path(found).resolve())
    raise FileNotFoundError(f"Executable not found: {executable}")


def _batch_quote(argument: str) -> str:
    # cmd expands percent/bang even inside quotes. Fail closed instead of changing argv.
    # Embedded quotes have no general lossless representation through arbitrary batch shims.
    if any(character in argument for character in ('"', "%", "!", "\r", "\n", "\x00")):
        raise ValueError("Unsafe batch argument (quote, expansion, or control character)")
    trailing = len(argument) - len(argument.rstrip("\\"))
    return '"' + argument + "\\" * trailing + '"'


def command_line(argv: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    if not argv:
        raise ValueError("Empty command")
    executable = resolve_executable(argv[0], env)
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        if os.name != "nt":
            raise OSError("Batch files require Windows")
        environment = os.environ if env is None else env
        cmd = resolve_executable(environment.get("COMSPEC", "cmd.exe"), env)
        quoted = " ".join(_batch_quote(arg) for arg in [executable, *argv[1:]])
        return [cmd, "/d", "/s", "/v:off", "/c", '"' + quoted + '"']
    return [executable, *argv[1:]]


def _parse_proc_stat(data: str) -> tuple[int, int]:
    """Return pid and parent pid from procfs stat, tolerating ')' in comm."""
    opening = data.find(" (")
    closing = data.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError("Malformed proc stat")
    pid = int(data[:opening])
    fields = data[closing + 1:].split()
    if len(fields) < 2:
        raise ValueError("Malformed proc stat")
    return pid, int(fields[1])


def _posix_descendants(root_pid: int) -> list[int]:
    parents: dict[int, int] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if entry.name.isdigit():
                try:
                    pid, parent = _parse_proc_stat((entry / "stat").read_text(encoding="utf-8"))
                    parents[pid] = parent
                except (OSError, ValueError):
                    continue
    else:
        try:
            listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                                     text=True, timeout=2, check=False)
            for line in listing.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2:
                    try:
                        parents[int(fields[0])] = int(fields[1])
                    except ValueError:
                        continue
        except (OSError, subprocess.TimeoutExpired):
            return []
    found: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier} - found - {root_pid}
        found.update(children)
        frontier = children
    return list(found)


def _environ_has_process_token(data: bytes, token: str) -> bool:
    """Check procfs environ bytes for the exact ReOEngine process token entry."""
    expected = b"REOENGINE_PROCESS_TOKEN=" + token.encode("ascii")
    return expected in data.split(b"\0")


def _linux_token_processes(token: str) -> list[int]:
    proc = Path("/proc")
    if sys.platform != "linux" or not proc.is_dir():
        return []
    found: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if _environ_has_process_token((entry / "environ").read_bytes(), token):
                found.append(pid)
        except OSError:
            continue
    return found


def _kill_tree(process: subprocess.Popen[bytes], job: _WindowsJob | None = None,
               token: str | None = None) -> bool:
    descendants: list[int] = []
    if sys.platform == "win32":
        if job is not None:
            job.terminate()
        else:
            process.kill()
    else:
        descendants = list(set(_posix_descendants(process.pid)) |
                           (set(_linux_token_processes(token)) if token is not None else set()))
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if token is not None and sys.platform == "linux":
            for pid in set(_linux_token_processes(token)) - set(descendants):
                descendants.append(pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    if sys.platform != "win32":
        from .state import pid_alive
        return any(pid_alive(pid) for pid in descendants)
    return False


def run_process(
    argv: Sequence[str], *, cwd: Path, timeout: float,
    stdin_file: Path | None = None, stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None, tail_bytes: int = 4096,
    on_stdout: Callable[[bytes], None] | None = None,
    on_stderr: Callable[[bytes], None] | None = None,
    stdin_queue: Queue[str | None] | None = None,
    stop_event: threading.Event | None = None,
) -> ProcessResult:
    if timeout <= 0 or tail_bytes < 0:
        raise ValueError("timeout must be positive and tail_bytes nonnegative")
    if sum(item is not None for item in (stdin_file, stdin_text, stdin_bytes, stdin_queue)) > 1:
        raise ValueError("Choose one stdin source")
    process_token: str | None = None
    launch_env = env
    if os.name == "posix":
        launch_env = dict(os.environ if env is None else env)
        process_token = uuid.uuid4().hex
        launch_env["REOENGINE_PROCESS_TOKEN"] = process_token
    command = command_line(argv, launch_env)
    source: BinaryIO | None = stdin_file.open("rb") if stdin_file is not None else None
    started = time.monotonic()
    tails = [bytearray(), bytearray()]
    errors: list[BaseException] = []
    job: _WindowsJob | None = None

    def drain(stream: BinaryIO, index: int) -> None:
        try:
            while chunk := os.read(stream.fileno(), 65536):
                if index == 0 and on_stdout is not None:
                    on_stdout(chunk)
                if index == 1 and on_stderr is not None:
                    on_stderr(chunk)
                tails[index].extend(chunk)
                if len(tails[index]) > tail_bytes:
                    del tails[index][:len(tails[index]) - tail_bytes]
        except BaseException as exc:
            errors.append(exc)
        finally:
            stream.close()

    try:
        # Popen's list2cmdline escapes the quotes in /c's payload with backslashes;
        # cmd.exe has different quote rules, so pass that payload verbatim.
        launch: str | list[str] = command
        if Path(command[0]).name.lower() == "cmd.exe" and command[1:5] == ["/d", "/s", "/v:off", "/c"]:
            launch = subprocess.list2cmdline(command[:5]) + " " + command[5]
        creationflags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x4) if sys.platform == "win32" else 0
        process = subprocess.Popen(launch, cwd=cwd, env=launch_env, shell=False,
                                   stdin=source if source is not None else subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name != "nt",
                                   creationflags=creationflags)
        if os.name == "nt":
            try:
                job = _WindowsJob(process.pid)
            except BaseException:
                process.kill()
                process.wait()
                for stream in (process.stdout, process.stderr, process.stdin):
                    if stream is not None:
                        stream.close()
                raise
        assert process.stdout is not None and process.stderr is not None
        readers = [threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
        for reader in readers:
            reader.start()

        def feed() -> None:
            if source is None and process.stdin is not None:
                try:
                    if stdin_queue is None:
                        process.stdin.write(stdin_bytes if stdin_bytes is not None else (stdin_text or "").encode("utf-8"))
                        process.stdin.flush()
                    else:
                        while process.poll() is None:
                            try:
                                message = stdin_queue.get(timeout=0.05)
                            except Empty:
                                continue
                            if message is None:
                                break
                            process.stdin.write(message.encode("utf-8"))
                            process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    process.stdin.close()

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        timed_out = False
        kill_tree_incomplete = False
        deadline = started + timeout
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    kill_tree_incomplete = _kill_tree(process, job, process_token)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=min(0.05, remaining) if stop_event is not None else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if stop_event is None:
                        raise
            # A child may inherit stdout even after its parent exits.
            for reader in readers:
                reader.join(max(0.0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                raise subprocess.TimeoutExpired(command, timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree_incomplete = _kill_tree(process, job, process_token)
        for reader in readers:
            reader.join(10)
        writer.join(10)
        if errors:
            raise RuntimeError("Process output reader failed") from errors[0]
        if any(reader.is_alive() for reader in readers):
            raise RuntimeError("Process tree did not close output pipes")
        return ProcessResult(process.returncode, _decode_tail(tails[0], tail_bytes), _decode_tail(tails[1], tail_bytes),
                             time.monotonic() - started, timed_out, kill_tree_incomplete)
    finally:
        if job is not None:
            job.close()
        if source is not None:
            source.close()


def _decode_tail(data: bytearray, tail_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    # Replacement characters can expand invalid bytes; trim whole characters to fit.
    excess = len(text.encode("utf-8")) - tail_bytes
    start = 0
    while excess > 0:
        excess -= len(text[start].encode("utf-8"))
        start += 1
    return text[start:]
