from __future__ import annotations

import os
import sys
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import git_command, make_repository
from reoengine.adapters.api_json import _extract
from reoengine.config import Engine
from reoengine.gates import GateRequest, _execute
from reoengine.gitops import Git
from reoengine.guard import OutsideGuard, SnapshotError, source_snapshot
from reoengine.procs import resolve_executable, run_process
from tests.fakes.http_server import ScriptedHttpServer


class ReviewFixTests(unittest.TestCase):
    def test_relative_executable_resolves_against_child_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe = root / "bin" / ("tool.exe" if os.name == "nt" else "tool")
            exe.parent.mkdir()
            exe.write_text("tool", encoding="utf-8")
            self.assertEqual(Path(resolve_executable("bin/tool", cwd=root)), exe.resolve())

    def test_callback_error_kills_sleeping_child_promptly(self) -> None:
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "Process output reader failed"):
                run_process([sys.executable, "-c", "import os,time; os.write(1,b'x'); time.sleep(30)"],
                            cwd=Path(temporary), timeout=10, on_stdout=lambda _chunk: (_ for _ in ()).throw(ValueError("callback")))
        self.assertLess(time.monotonic() - started, 4)

    @unittest.skipUnless(os.name == "posix", "POSIX invocation process groups")
    def test_normal_exit_terminates_background_group_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file, late_file = root / "child.pid", root / "survived"
            child = ("import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); "
                     "time.sleep(0.5); pathlib.Path(sys.argv[2]).write_text('survived')")
            parent = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],"
                      "sys.argv[2],sys.argv[3]]); deadline=time.monotonic()+2; "
                      "while not __import__('pathlib').Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.01)")
            run_process([sys.executable, "-c", parent, child, str(pid_file), str(late_file)],
                        cwd=root, timeout=3)
            time.sleep(0.7)
            self.assertFalse(late_file.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows job object behavior")
    def test_job_resume_failure_kills_and_reaps_child(self) -> None:
        child = Mock(pid=1234, stdout=io.BytesIO(), stderr=io.BytesIO(), stdin=None)
        with tempfile.TemporaryDirectory() as temporary, \
                patch("reoengine.procs.subprocess.Popen", return_value=child), \
                patch("reoengine.procs._WindowsJob", side_effect=OSError("simulated NtResumeProcess failure")):
            with self.assertRaisesRegex(OSError, "simulated NtResumeProcess failure"):
                run_process([sys.executable, "-c", "pass"], cwd=Path(temporary), timeout=1)
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with()

    def test_wrapper_rejects_dot_link_name(self) -> None:
        request = GateRequest((), (), (), 1, False, "", isolation_mode="wrapper",
                             isolation_wrapper=("wrapper", "{link:..}"))
        result = _execute("test", ("true",), Path.cwd(), request)
        self.assertEqual(result.outcome, "infra_error")

    def test_finish_reasons_are_non_success(self) -> None:
        with self.assertRaises(ValueError):
            _extract(Engine.GEMINI, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]})
        with self.assertRaises(ValueError):
            _extract(Engine.ANTHROPIC, {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{}"}]})
        with self.assertRaises(ValueError):
            _extract(Engine.OPENAI, {"status": "incomplete", "output_text": "{}"})

    def test_http_body_read_obeys_total_deadline(self) -> None:
        from reoengine.adapters.api_json import ApiJsonAdapter
        from reoengine.adapters.base import ErrorKind
        from reoengine.config import RoleConfig
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"GEMINI_API_KEY": "offline-key"}):
            root = Path(temporary)
            prompt = root / "prompt.txt"
            prompt.write_text("prompt", encoding="utf-8")
            with ScriptedHttpServer([(200, b'{"candidates":[]}')]) as server:
                server.slow_drip = True
                role = RoleConfig(Engine.GEMINI, "fake", base_url=server.base_url)
                started = time.monotonic()
                result = ApiJsonAdapter(Engine.GEMINI).invoke(role, prompt, root, 1)
                elapsed = time.monotonic() - started
            self.assertEqual(result.error, ErrorKind.TIMEOUT)
            self.assertLess(elapsed, 2)

    def test_unsafe_api_urls_fail_before_sending_key(self) -> None:
        from reoengine.adapters.api_json import ApiJsonAdapter
        from reoengine.adapters.base import ErrorKind
        from reoengine.config import RoleConfig
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"GEMINI_API_KEY": "offline-key"}):
            root = Path(temporary)
            prompt = root / "prompt.txt"
            prompt.write_text("prompt", encoding="utf-8")
            for base_url in ("http://api.example.invalid", "https://user:pass@example.invalid", "https://example.invalid/#fragment"):
                role = RoleConfig(Engine.GEMINI, "fake", base_url=base_url)
                with patch("reoengine.adapters.api_json._OPENER.open") as opener:
                    result = ApiJsonAdapter(Engine.GEMINI).invoke(role, prompt, root, 1)
                self.assertEqual(result.error, ErrorKind.PROCESS)
                opener.assert_not_called()

    def test_events_appended_during_call_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = make_repository(root)
            run, config, state = (root / "run", root / "config", root / "state")
            for path in (run, config, state):
                path.mkdir(exist_ok=True)
            guard = OutsideGuard(run, config, state, Git(repo))
            before = guard.snapshot()
            events = run / "events.jsonl"
            events.write_bytes(b'{"forged":true}\n')
            after = guard.snapshot()
            self.assertTrue(any(v.category == "engine_state" for v in guard.compare(before, after)))

    def test_small_git_hook_content_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = make_repository(root)
            hook = repo / os.fsdecode(git_command(repo, "rev-parse", "--git-dir").strip()) / "hooks" / "sample"
            hook.parent.mkdir(exist_ok=True)
            hook.write_bytes(b"aaaa")
            git = Git(repo)
            before = source_snapshot(git)
            self.assertIsNotNone(before)
            stamp = hook.stat()
            hook.write_bytes(b"bbbb")
            os.utime(hook, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            after = source_snapshot(git)
            self.assertIsNotNone(after)
            self.assertNotEqual(before, after)

    def test_collect_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = make_repository(root)
            run, config, state = (root / "run", root / "config", root / "state")
            for path in (run, config, state):
                path.mkdir(exist_ok=True)
            (config / "small").write_text("x", encoding="utf-8")
            with patch("reoengine.guard.MAX_COLLECT_ENTRIES", 1):
                with self.assertRaises(SnapshotError):
                    OutsideGuard(run, config, state, Git(repo)).snapshot()


if __name__ == "__main__":
    unittest.main()
