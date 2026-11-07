from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reoengine import state
from reoengine._data import append_line, atomic_write, encode, read_lines
from reoengine.adapters import AgyAdapter, ClaudeAdapter, CodexAdapter, ErrorKind, codex_rate_limits
from reoengine.config import (Effort, Engine, QuotaConfig, RoleConfig, Sandbox, effective_roles, load_configuration,
                          load_limits, load_profile, load_project, load_roles)
from reoengine.events import Event, EventKind, EventLog, LimitRemaining, sha256_file
from reoengine.procs import command_line, resolve_executable, run_process
from reoengine.schemas import SchemaError, load_schema, validate, validate_artifact
from reoengine.state import RunLock, RunLockedError, RunState, Stage, Status, load_state, save_state
from reoengine.usage import TokenUsage, UsageLog, UsageRecord

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fakes" / "agent.py"


class TemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        # Every test artifact stays inside the authorized engine tree and is removed.
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "engine", prefix="test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.prompt = self.directory / "prompt.txt"
        self.payload = ('Кириллица "кавычки" % ! & | < > ^\n' * 10000).encode("utf-8")[:204800]
        self.prompt.write_bytes(self.payload)

    def command(self, engine: str, mode: str = "success") -> tuple[str, ...]:
        return (sys.executable, str(FAKE), engine, mode)


class ProcessTests(TemporaryTest):
    def test_large_stdin_file(self) -> None:
        output = self.directory / "response.json"
        result = run_process([*self.command("codex"), "--output-last-message", str(output)],
                             cwd=self.directory, timeout=5, stdin_file=self.prompt)
        self.assertEqual(result.exit_code, 0)
        message = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(message["size"], 204800)
        self.assertEqual(message["hash"], hashlib.sha256(self.payload).hexdigest())
        self.assertNotIn(self.payload[:100].decode("utf-8", errors="ignore"), str(message["argv"]))

    def test_stdin_string_and_bounded_tails(self) -> None:
        result = run_process(self.command("codex", "tails"), cwd=self.directory, timeout=5,
                             stdin_text='"Я"' * 100000)
        self.assertEqual(result.exit_code, 0)
        self.assertLessEqual(len(result.stdout_tail.encode("utf-8")), 4096)
        self.assertLessEqual(len(result.stderr_tail.encode("utf-8")), 4096)
        self.assertEqual(result.stdout_tail, "я" * 2048)
        self.assertEqual(result.stderr_tail, "ю" * 2048)

    def test_utf8_stdin_and_invalid_output(self) -> None:
        text = '"Я"'
        for tail_bytes, expected in ((4096, text + "\ufffd"), (6, 'Я"\ufffd'),
                                     (5, '"\ufffd'), (3, "\ufffd"), (2, ""), (0, "")):
            with self.subTest(tail_bytes=tail_bytes):
                result = run_process(self.command("codex", "echo_invalid_utf8"), cwd=self.directory,
                                     timeout=5, stdin_text=text, tail_bytes=tail_bytes)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.stdout_tail, expected)
                self.assertEqual(result.stderr_tail, expected)
                self.assertLessEqual(len(result.stdout_tail.encode("utf-8")), tail_bytes)
                self.assertLessEqual(len(result.stderr_tail.encode("utf-8")), tail_bytes)

    def test_timeout_kills_tree(self) -> None:
        for mode in ("tree", "tree_parent_exit"):
            with self.subTest(mode=mode):
                marker = self.directory / f"{mode}.txt"
                result = run_process([*self.command("codex", mode), str(marker)], cwd=self.directory,
                                     timeout=0.5, stdin_text="")
                self.assertTrue(result.timed_out)
                child_pid = int(marker.with_suffix(".pid").read_text())
                self.assertFalse(state.pid_alive(child_pid))
                time.sleep(1.6)
                self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX process sessions")
    def test_timeout_kills_setsid_descendant(self) -> None:
        marker = self.directory / "setsid.txt"
        result = run_process([*self.command("codex", "tree_setsid"), str(marker)], cwd=self.directory,
                             timeout=0.5, stdin_text="")
        self.assertTrue(result.timed_out)
        child_pid = int(marker.with_suffix(".pid").read_text())
        self.assertFalse(state.pid_alive(child_pid))
        self.assertFalse(marker.exists())

    def test_missing_executable(self) -> None:
        with self.assertRaises(FileNotFoundError):
            resolve_executable(str(self.directory / "does-not-exist"))

    @unittest.skipUnless(os.name == "nt", "Windows batch shim")
    def test_cmd_shim_and_pathext(self) -> None:
        with patch.dict(os.environ, {"REOENGINE_TEST_PYTHON":sys.executable}):
            output = self.directory / "кириллица & space.json"
            shim_directory = self.directory / "shim space & кириллица"
            shim_directory.mkdir()
            shim = shim_directory / "echo_stdin.cmd"
            shim.write_bytes((FAKE.parent / "echo_stdin.cmd").read_bytes())
            (shim_directory / "agent.py").write_bytes(FAKE.read_bytes())
            result = run_process([str(shim), "--output-last-message", str(output), "", "literal & value", "trailing\\"],
                                 cwd=self.directory, timeout=5, stdin_file=self.prompt)
            self.assertEqual(result.exit_code, 0, result.stderr_tail)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["size"], 204800)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["argv"][-3:], ["", "literal & value", "trailing\\"])
            env = dict(os.environ, PATH=str(shim_directory), PATHEXT=".CMD")
            self.assertEqual(Path(resolve_executable("echo_stdin", env)), shim)
            for unsafe in ['a"b', "%PATH%", "!PATH!", "line\nbreak"]:
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    command_line([str(shim), unsafe])


class AdapterTests(TemporaryTest):
    def setUp(self) -> None:
        super().setUp()
        boundary = self.directory

        # Keep real files inside engine/, but model an independent filesystem root
        # so the repository's own CLAUDE.md cannot contaminate synthetic scenarios.
        # exists() and resolve() retain their real filesystem implementations.
        class IsolatedPath(type(boundary)):
            @property
            def parents(self) -> tuple[Path, ...]:
                return tuple(parent for parent in super().parents if parent.is_relative_to(boundary))

            @classmethod
            def home(cls) -> Path:
                return cls(boundary / "home")

        self.directory = IsolatedPath(boundary)
        self.home = IsolatedPath.home()
        paths = patch("reoengine.adapters.claude.Path", IsolatedPath)
        paths.start()
        self.addCleanup(paths.stop)
        path_helpers = patch("reoengine.paths.Path", IsolatedPath)
        path_helpers.start()
        self.addCleanup(path_helpers.stop)

    def test_codex_success_flags_usage_and_stdin(self) -> None:
        role = RoleConfig(Engine.CODEX, "synthetic-model", sandbox=Sandbox.WORKSPACE_WRITE)
        result = CodexAdapter(self.command("codex", "large")).invoke(role, self.prompt, self.directory, 5)
        self.assertTrue(result.success, result)
        self.assertEqual(result.usage, TokenUsage(100, 20, 15, 7))
        self.assertLessEqual(len(result.stdout_tail.encode("utf-8")), 4096)
        message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
        self.assertEqual(message["hash"], sha256_file(self.prompt))
        self.assertIn("--json", message["argv"])
        self.assertEqual(message["argv"][-1], "-")
        self.assertIn("approval_policy='never'", message["argv"])

    def test_codex_protocol_failures(self) -> None:
        role = RoleConfig(Engine.CODEX, "synthetic-model")
        for mode in ("failed", "error", "garbage", "missing_completed", "missing_output"):
            with self.subTest(mode=mode):
                result = CodexAdapter(self.command("codex", mode)).invoke(role, self.prompt, self.directory, 5)
                self.assertFalse(result.success)
                self.assertEqual(result.error, ErrorKind.PROTOCOL)

    def test_codex_neutral_directory_skips_git_check(self) -> None:
        neutral = self.directory / "neutral"
        neutral.mkdir()
        role = RoleConfig(Engine.CODEX, "synthetic-model", sandbox=Sandbox.READ_ONLY)
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.directory)}):
            result = CodexAdapter(self.command("codex")).invoke(role, self.prompt, neutral, 5)
        self.assertTrue(result.success, result)
        message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
        self.assertTrue(message["empty_cwd"])
        self.assertEqual(Path(message["cwd"]), neutral)
        self.assertIn("--skip-git-repo-check", message["argv"])
        self.assertEqual(message["argv"][message["argv"].index("--sandbox") + 1], "read-only")

    def test_codex_git_directory_and_git_file_do_not_skip_check(self) -> None:
        role = RoleConfig(Engine.CODEX, "synthetic-model", sandbox=Sandbox.WORKSPACE_WRITE)
        for separate in (False, True):
            with self.subTest(separate_git_dir=separate):
                repo = self.directory / f"repo-{separate}"
                repo.mkdir()
                command = ["git", "init", "--quiet"]
                if separate:
                    command.extend(["--separate-git-dir", str(self.directory / "git-dir")])
                subprocess.run(command, cwd=repo, check=True, shell=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual((repo / ".git").is_file(), separate)
                nested = repo / "nested"
                nested.mkdir()
                result = CodexAdapter(self.command("codex")).invoke(role, self.prompt, nested, 5)
                self.assertTrue(result.success, result)
                message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
                self.assertNotIn("--skip-git-repo-check", message["argv"])
                self.assertEqual(message["argv"][message["argv"].index("--sandbox") + 1], "workspace-write")

    def test_codex_git_detection_failure_skips_check(self) -> None:
        role = RoleConfig(Engine.CODEX, "synthetic-model")
        errors = (FileNotFoundError("synthetic missing git"), ValueError("synthetic invalid cwd"),
                  subprocess.TimeoutExpired("git", 5))
        for error in errors:
            with self.subTest(error=error), patch("reoengine.adapters.codex.subprocess.run", side_effect=error):
                result = CodexAdapter(self.command("codex")).invoke(role, self.prompt, self.directory, 5)
                self.assertTrue(result.success, result)
                message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
                self.assertIn("--skip-git-repo-check", message["argv"])

    def test_all_adapters_errors(self) -> None:
        for engine, adapter_class in ((Engine.CODEX, CodexAdapter), (Engine.AGY, AgyAdapter), (Engine.CLAUDE, ClaudeAdapter)):
            for mode, expected in (("quota", ErrorKind.QUOTA), ("quota_long_stderr", ErrorKind.QUOTA),
                                   ("failed", ErrorKind.PROTOCOL), ("timeout", ErrorKind.TIMEOUT),
                                   ("garbage", ErrorKind.PROTOCOL), ("process", ErrorKind.PROCESS)):
                with self.subTest(engine=engine, mode=mode):
                    command = self.command(engine.value, mode)
                    if engine == Engine.CLAUDE:
                        adapter = adapter_class(self.directory / "neutral", command)
                    elif engine == Engine.AGY:
                        adapter = adapter_class(command, capabilities=("--sandbox", "--mode"))
                    else:
                        adapter = adapter_class(command)
                    role = RoleConfig(engine, "opus" if engine == Engine.CLAUDE else "synthetic-model", neutral_cwd=engine == Engine.CLAUDE)
                    result = adapter.invoke(role, self.prompt, self.directory, 0.2 if mode == "timeout" else 5)
                    self.assertEqual(result.error, expected, result)
                    self.assertFalse(result.success)

    def test_quota_patterns_and_exit_codes_are_configurable(self) -> None:
        role = RoleConfig(Engine.CODEX, "synthetic")
        for quota in (QuotaConfig((3,), ("quota",)), QuotaConfig((29,), ("different-pattern",))):
            result = CodexAdapter(self.command("codex", "quota"), quota=quota).invoke(role, self.prompt, self.directory, 5)
            self.assertEqual(result.error, ErrorKind.PROCESS)

    def test_agy_json_usage_and_quota_text_is_data(self) -> None:
        role = RoleConfig(Engine.AGY, "synthetic-model", disable_skills=True)
        for mode in ("success", "large", "answer_quota", "stderr_only"):
            result = AgyAdapter(self.command("agy", mode), capabilities=("--sandbox", "--mode")).invoke(
                role, self.prompt, self.directory, 5)
            self.assertTrue(result.success, result)
            self.assertEqual(result.usage, TokenUsage(100, 21, 15, 8, 0.01, 0.25, "reported"))
            message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
            self.assertEqual(message["hash"], sha256_file(self.prompt))
            self.assertIn("--disable-slash-commands", message["argv"])
            self.assertNotIn("--print", message["argv"])
            self.assertNotIn("-p", message["argv"])

    def test_claude_is_neutral_opus_only(self) -> None:
        role = RoleConfig(Engine.CLAUDE, "opus", neutral_cwd=True, note="quota_shared_with_chat")
        result = ClaudeAdapter(self.directory / "neutral", self.command("claude")).invoke(role, self.prompt, self.directory, 5)
        self.assertTrue(result.success, result)
        self.assertEqual(result.note, "quota_shared_with_chat")
        self.assertEqual(result.usage.cached_input_tokens, 22)
        self.assertEqual(result.usage.cost_usd, 0.01)
        message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
        self.assertTrue(message["empty_cwd"])
        self.assertNotEqual(Path(message["cwd"]), self.directory)
        self.assertEqual(message["argv"], ["-p", "--model", "opus", "--tools", "", "--permission-mode", "plan", "--output-format", "json"])
        with self.assertRaises(ValueError):
            ClaudeAdapter(self.directory / "neutral", self.command("claude")).invoke(replace(role, model="other"), self.prompt, self.directory, 5)

    def test_claude_rejects_instruction_ancestors_before_launch(self) -> None:
        neutral = self.directory / "parent" / "nested" / "neutral"
        for ancestor in (self.directory, neutral.parent, neutral):
            for relative in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
                instruction = ancestor / relative
                instruction.parent.mkdir(parents=True, exist_ok=True)
                instruction.write_text("synthetic project instructions", encoding="utf-8")
                try:
                    with self.subTest(instruction=instruction), patch("reoengine.adapters.claude.invoke_json") as invoke:
                        with self.assertRaisesRegex(ValueError, "inherits project instructions") as error:
                            ClaudeAdapter(neutral, self.command("claude"))
                        self.assertIn(str(instruction), str(error.exception))
                        invoke.assert_not_called()
                finally:
                    instruction.unlink()

    def test_claude_rechecks_ancestors_at_invocation(self) -> None:
        adapter = ClaudeAdapter(self.directory / "neutral", self.command("claude"))
        (self.directory / "CLAUDE.local.md").write_text("synthetic", encoding="utf-8")
        role = RoleConfig(Engine.CLAUDE, "opus", neutral_cwd=True)
        with patch("reoengine.adapters.claude.invoke_json") as invoke:
            with self.assertRaisesRegex(ValueError, "inherits project instructions"):
                adapter.invoke(role, self.prompt, self.directory, 5)
            invoke.assert_not_called()

    def test_claude_checks_ancestors_above_test_directory(self) -> None:
        nested = self.directory / "nested-test-directory"
        for relative in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            instruction = self.directory / relative
            instruction.parent.mkdir(parents=True, exist_ok=True)
            instruction.write_text("synthetic ancestor instructions", encoding="utf-8")
            try:
                with self.subTest(relative=relative):
                    with self.assertRaisesRegex(ValueError, "inherits project instructions") as error:
                        ClaudeAdapter(nested / "neutral", self.command("claude"))
                    self.assertIn(str(instruction), str(error.exception))
            finally:
                instruction.unlink()

    def test_claude_rejects_real_project_ancestor(self) -> None:
        instruction = ROOT / "CLAUDE.md"
        if not instruction.exists():
            self.skipTest("Repository has no CLAUDE.md")
        # A native Path traverses the actual ancestors, outside the synthetic root.
        with self.assertRaisesRegex(ValueError, "inherits project instructions") as error:
            ClaudeAdapter(Path(self.directory / "neutral"), self.command("claude"))
        self.assertIn(str(instruction), str(error.exception))

    def global_claude_instructions(self) -> Path:
        instruction = self.home / ".claude" / "CLAUDE.md"
        instruction.parent.mkdir(parents=True, exist_ok=True)
        instruction.write_text("synthetic global instructions", encoding="utf-8")
        return instruction

    def test_claude_allows_global_home_instructions(self) -> None:
        instruction = self.global_claude_instructions()
        neutral = self.home / "AppData" / "Local" / "reoengine" / "neutral"
        neutral.mkdir(parents=True)
        adapter = ClaudeAdapter(neutral, self.command("claude"))
        self.assertEqual(adapter.neutral_directory, neutral.resolve())
        self.assertTrue(instruction.exists())

    def test_claude_global_exception_does_not_allow_project_instructions(self) -> None:
        self.global_claude_instructions()
        neutral = self.home / "AppData" / "Local" / "reoengine" / "neutral"
        for ancestor in (self.home, neutral.parent):
            for relative in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
                if ancestor == self.home and relative == ".claude/CLAUDE.md":
                    continue
                instruction = ancestor / relative
                instruction.parent.mkdir(parents=True, exist_ok=True)
                instruction.write_text("synthetic project instructions", encoding="utf-8")
                try:
                    with self.subTest(instruction=instruction):
                        with self.assertRaisesRegex(ValueError, "inherits project instructions") as error:
                            ClaudeAdapter(neutral, self.command("claude"))
                        self.assertIn(str(instruction), str(error.exception))
                finally:
                    instruction.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive paths")
    def test_claude_global_home_comparison_ignores_case_on_windows(self) -> None:
        self.global_claude_instructions()
        neutral = self.home / "AppData" / "Local" / "reoengine" / "neutral"
        with patch("reoengine.adapters.claude.Path.home", return_value=Path(str(self.home).upper())):
            ClaudeAdapter(neutral, self.command("claude"))

    def test_claude_default_directory_uses_localappdata(self) -> None:
        self.global_claude_instructions()
        local_app_data = self.home / "AppData" / "Local"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
            adapter = ClaudeAdapter(command=self.command("claude"))
        self.assertEqual(adapter.neutral_directory, local_app_data / "reoengine" / "neutral")
        result = adapter.invoke(RoleConfig(Engine.CLAUDE, "opus", neutral_cwd=True), self.prompt, self.directory, 5)
        self.assertTrue(result.success, result)
        message = json.loads(result.final_message_path.read_text(encoding="utf-8"))
        self.assertEqual(Path(message["cwd"]).parent, adapter.neutral_directory)
        self.assertTrue(message["empty_cwd"])

    def test_claude_default_directory_uses_home_state_path_without_localappdata(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            adapter = ClaudeAdapter()
            self.assertEqual(adapter.neutral_directory, self.home / ".local" / "state" / "reoengine" / "neutral")
            ClaudeAdapter(self.directory / "neutral", self.command("claude"))

    def test_response_schema(self) -> None:
        schema = self.directory / "schema.json"
        atomic_write(schema, {"type":"object", "properties":{}, "additionalProperties":False})
        for engine, constructor in ((Engine.CODEX, CodexAdapter), (Engine.AGY, AgyAdapter)):
            options = {"capabilities": ("--sandbox", "--mode")} if engine == Engine.AGY else {}
            result = constructor(self.command(engine.value), **options).invoke(
                RoleConfig(engine, "synthetic"), self.prompt, self.directory, 5, schema)
            self.assertEqual(result.error, ErrorKind.SCHEMA)

    def test_codex_via_cmd(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows batch shim")
        with patch.dict(os.environ, {"REOENGINE_TEST_PYTHON":sys.executable}):
            result = CodexAdapter((str(FAKE.parent / "echo_stdin.cmd"),)).invoke(RoleConfig(Engine.CODEX, "synthetic"), self.prompt, self.directory, 5)
        self.assertTrue(result.success, result)

    def test_rate_limits_success_and_soft_failure(self) -> None:
        log = EventLog(self.directory)
        result = codex_rate_limits(cwd=self.directory, event_log=log, run_id="test", command=self.command("rpc"))
        self.assertEqual(result, LimitRemaining(75, 40))
        start = time.monotonic()
        self.assertEqual(codex_rate_limits(cwd=self.directory, event_log=log, run_id="test",
                                          command=self.command("rpc", "linger"), timeout=3), LimitRemaining(75, 40))
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(log.path.exists())
        for mode in ("garbage", "process", "timeout", "rpc_error", "bad_window"):
            self.assertIsNone(codex_rate_limits(cwd=self.directory, event_log=log, run_id="test", command=self.command("rpc", mode), timeout=0.2 if mode == "timeout" else 5))
        events = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 5)
        self.assertTrue(all(item["event"] == "rate_limits_unavailable" for item in events))
        self.assertNotIn("synthetic limit", log.path.read_text(encoding="utf-8"))


class ConfigurationTests(TemporaryTest):
    def test_defaults_all_roles_and_cross_engine_fallback(self) -> None:
        (self.directory / "node_modules").mkdir()
        with patch.dict(os.environ, {"EXAMPLE_REPO": str(self.directory)}):
            cfg = load_configuration(ROOT / "config")
        self.assertEqual(len(cfg.roles), 15)
        self.assertEqual(cfg.roles["coder"].model, "gpt-6.1-sol")
        self.assertEqual(cfg.roles["researcher"].replacements()[0].engine, Engine.CODEX)
        architect = cfg.roles["architect"]
        # Opus uses the Claude quota directly, not agy.
        self.assertEqual((architect.engine, architect.model, architect.neutral_cwd), (Engine.CLAUDE, "opus", True))
        self.assertEqual(architect.replacements(), ())
        self.assertEqual(cfg.limits.slots["coder"], 1)
        self.assertEqual(cfg.limits.max_fix_rounds, 3)
        self.assertEqual(cfg.limits.fix_loop_token_budget, 400_000)
        self.assertTrue(cfg.limits.fix_loop_no_progress_stop)
        self.assertEqual(cfg.limits.failure_ttl_days, 7)

    def test_engine_version_matches_package_metadata(self) -> None:
        metadata = tomllib.loads((ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8"))
        from reoengine import __version__
        self.assertEqual(metadata["project"]["version"], __version__)

    def test_override_precedence_and_unknown_key(self) -> None:
        roles = load_roles(ROOT / "config" / "roles.toml")
        tuning, overrides = self.directory / "tuning.json", self.directory / "overrides.json"
        atomic_write(tuning, {"roles":{"coder":{"model":"tuned", "effort":"low"}}})
        atomic_write(overrides, {"roles":{"coder":{"model":"manual"}}})
        effective = effective_roles(roles, tuning, overrides)
        self.assertEqual(effective["coder"].model, "manual")
        self.assertEqual(effective["coder"].effort, Effort.LOW)
        self.assertEqual(roles["coder"].model, "gpt-6.1-sol")
        for bad in ({"roles":{"coder":{"unknown":True}}}, {"roles":{"missing":{"model":"x"}}}, {"unknown":{}}):
            atomic_write(overrides, bad)
            with self.assertRaises(ValueError):
                effective_roles(roles, tuning, overrides)

    def test_strict_toml(self) -> None:
        path = self.directory / "input.toml"
        cases = ((load_roles, '[roles.coder]\nengine="codex"\nmodel="x"\nunknown=1'),
                 (load_roles, '[roles.coder]\nengine="other"\nmodel="x"'),
                 (load_limits, 'unknown=1'), (load_limits, '[slots]\ncoder=2'),
                 (load_limits, 'timeout_seconds=true'), (load_limits, 'max_slots=9'),
                 (load_limits, 'failure_ttl_days=0'),
                 (load_limits, 'fix_loop_token_budget=-1'),
                 (load_limits, 'fix_loop_no_progress_stop=1'),
                 (load_limits, '[quota]\nstderr_patterns=["["]'),
                 (load_profile, 'max_skills=9'), (load_profile, 'unknown=1'),
                 (load_profile, 'checks=["python -m unittest"]'),
                 (load_project, 'repo="relative"\nprofile="docs"'),
                 (load_project, 'repo="C:/synthetic"\nprofile="docs"\nunknown=1'))
        for loader, text in cases:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                loader(path)

    def test_profiles_projects_and_references(self) -> None:
        cfg = self.directory / "config"
        cfg.mkdir()
        (cfg / "profiles").mkdir()
        (cfg / "projects").mkdir()
        for name in ("roles.toml", "limits.toml"):
            (cfg / name).write_bytes((ROOT / "config" / name).read_bytes())
        rules = "Keep these rules verbatim.\nSecond line."
        (cfg / "profiles" / "docs.toml").write_text('skills=["one"]\nrules="""'+rules+'"""\nchecks=[["python","-V"]]\n', encoding="utf-8")
        repo = self.directory.as_posix()
        project = cfg / "projects" / "synthetic.toml"
        project.write_text(f'repo="{repo}"\nprofile="docs"\nallowed_paths=["src/**"]\n[offline_env]\nAPI_HOST="127.0.0.1:9"\n', encoding="utf-8")
        loaded = load_configuration(cfg)
        self.assertEqual(loaded.profiles["docs"].rules, rules)
        self.assertEqual(loaded.projects["synthetic"].offline_env["API_HOST"], "127.0.0.1:9")
        project.write_text(f'repo="{repo}"\nprofile="missing"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_configuration(cfg)


class SchemaTests(unittest.TestCase):
    def examples(self) -> dict[str, dict[str, object]]:
        checks = ["unit-tests"]
        gates = {"isolation":"env-only","passed":True,"checks":[{"name":"test","passed":True,"exit_code":0,"stdout_tail":"","stderr_tail":"","duration_ms":2,"outcome":"pass"}]}
        review = {"approved":True,"summary":"synthetic","findings":[]}
        return {"task":{"id":"S1","title":"synthetic","description":"example","requirements":[{"id":"R1","description":"works","verification":"unit test"}],"allowed_paths":["src/**"],"acceptance_checks":checks,"tdd_gate":True,"profile":"docs"},
                "plan":{"approach":"example","steps":[{"id":"1","description":"work","role":None}],"allowed_paths":["src/**"],"acceptance_checks":checks,"risks":[]},
                "research":{"summary":"example","sources":[{"title":"Synthetic source","url":"offline:synthetic"}],"findings":["example"],"unknowns":[]},
                "review":review,"gates":gates,
                "result":{"run_id":"test","status":"awaiting_human","base_sha":"a"*40,"head_sha":"b"*40,"changed_files":["src/x.py"],"warnings":[],"gates":gates,"review":review,"attempts":1,"usage_summary":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":None,"cost_usd":None},"artifacts":["diff.patch"]}}

    def test_valid_and_invalid_artifacts(self) -> None:
        for name, value in self.examples().items():
            with self.subTest(name=name):
                validate_artifact(name, value)
                with self.assertRaises(SchemaError):
                    validate_artifact(name, dict(value, extra="forbidden"))
                missing = dict(value)
                del missing[next(iter(missing))]
                with self.assertRaises(SchemaError):
                    validate_artifact(name, missing)
        result = self.examples()["result"]
        result["review"] = None
        validate_artifact("result", result)

    def test_nested_types_boolean_integer_enum(self) -> None:
        for value, definition in ((True,{"type":"integer"}), (1,{"type":"boolean"}),
                                   ([1],{"type":"array","items":{"type":"string"}}),
                                   ("bad",{"type":"string","enum":["good"]}),
                                   (True,{"type":"boolean","enum":[1]})):
            with self.subTest(value=value), self.assertRaises(SchemaError):
                validate(value, definition)
        review = self.examples()["review"]
        review["findings"] = [{"path":"src/a.py","line":None,"severity":"unknown","text":"example"}]
        with self.assertRaises(SchemaError):
            validate_artifact("review", review)
        with self.assertRaises(SchemaError):
            load_schema("../private")
        with self.assertRaises(SchemaError):
            validate({}, {"type":"object","properties":{"unused":{"type":"string","unsupported":True}}})
        validate(1.0, {"type":"integer", "enum":[1]})


class StateAndLogTests(TemporaryTest):
    def test_atomic_write_retries_permission_error(self) -> None:
        path = self.directory / "state.json"
        atomic_write(path, {"attempt": 1})
        real_replace = os.replace
        calls = 0

        def interrupted_replace(source: str, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise PermissionError("synthetic locked destination")
            real_replace(source, destination)

        with patch("reoengine._data._ATOMIC_REPLACE_ATTEMPTS", 20), \
                patch("reoengine._data.os.replace", side_effect=interrupted_replace) as replacing, \
                patch("reoengine._data.time.sleep") as sleeping:
            atomic_write(path, {"attempt": 2})
        self.assertEqual(replacing.call_count, 4)
        self.assertEqual(sleeping.call_args_list, [call(0.05)] * 3)
        self.assertEqual(path.read_bytes(), encode({"attempt": 2}))
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_atomic_write_exhausted_permission_error_retries(self) -> None:
        path = self.directory / "state.json"
        original = encode({"attempt": 1})
        path.write_bytes(original)
        errors = [PermissionError(f"synthetic locked destination {attempt}") for attempt in range(20)]
        with patch("reoengine._data._ATOMIC_REPLACE_ATTEMPTS", 20), \
                patch("reoengine._data.os.replace", side_effect=errors) as replacing, \
                patch("reoengine._data.time.sleep") as sleeping:
            with self.assertRaises(PermissionError) as raised:
                atomic_write(path, {"attempt": 2})
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(replacing.call_count, 20)
        self.assertEqual(sleeping.call_args_list, [call(0.05)] * 19)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_atomic_write_immediate_replace_errors(self) -> None:
        path = self.directory / "state.json"
        cases = ((1, PermissionError("synthetic POSIX permission error")),
                 (20, OSError("synthetic interrupted replace")))
        for attempts, error in cases:
            with self.subTest(attempts=attempts, error=error), \
                    patch("reoengine._data._ATOMIC_REPLACE_ATTEMPTS", attempts), \
                    patch("reoengine._data.os.replace", side_effect=error) as replacing, \
                    patch("reoengine._data.time.sleep") as sleeping:
                with self.assertRaises(type(error)) as raised:
                    atomic_write(path, {"attempt": 1})
                self.assertIs(raised.exception, error)
                replacing.assert_called_once()
                sleeping.assert_not_called()
                self.assertFalse(path.exists())
                self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_atomic_state_and_version(self) -> None:
        initial = RunState("test", Stage.IMPLEMENT, Status.RUNNING, 1, "base", "head", {"plan":"a"*64})
        save_state(self.directory, initial)
        self.assertEqual(load_state(self.directory), initial)
        with patch("reoengine._data.os.replace", side_effect=OSError("synthetic interrupted replace")):
            with self.assertRaises(OSError):
                save_state(self.directory, replace(initial, attempt=2))
        self.assertEqual(load_state(self.directory), initial)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        with self.assertRaises(ValueError):
            save_state(self.directory, replace(initial, schema_version=99))

    def test_lock_exclusion_stale_and_live_pid(self) -> None:
        self.assertTrue(state.pid_alive(os.getpid()))
        with RunLock(self.directory):
            for remove_stale in (False, True):
                with self.assertRaises(RunLockedError):
                    RunLock(self.directory, remove_stale=remove_stale).acquire()
        self.assertFalse((self.directory / "run.lock").exists())
        # Obtain a real PID which is now dead, without assuming a special invalid PID.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(state.pid_alive(child.pid))
        atomic_write(self.directory / "run.lock", {"pid":child.pid,"ts":"synthetic","token":"old"})
        with self.assertRaises(RunLockedError):
            RunLock(self.directory).acquire()
        with RunLock(self.directory, remove_stale=True):
            self.assertEqual(json.loads((self.directory / "run.lock").read_text())["pid"], os.getpid())

    def test_events_hashes_append_only_and_no_contents(self) -> None:
        log = EventLog(self.directory)
        digest = sha256_file(self.prompt)
        event = Event("test", EventKind.STAGE_START, Stage.IMPLEMENT, "coder", "synthetic", Engine.CODEX,
                      0, 10, digest, digest, LimitRemaining(75, 40))
        log.append(event)
        first = log.path.read_bytes()
        log.append(replace(event, event=EventKind.STAGE_END))
        self.assertTrue(log.path.read_bytes().startswith(first))
        records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["input_hash"], hashlib.sha256(self.payload).hexdigest())
        self.assertNotIn("Кириллица", log.path.read_text(encoding="utf-8"))
        self.assertEqual(set(records[0]), {"ts","run_id","stage","role","model","engine","exit_code","duration_ms","input_hash","output_hash","limit_remaining","event"})

    def usage_record(self, when: datetime) -> UsageRecord:
        return UsageRecord("test", Stage.IMPLEMENT, "coder", Engine.CODEX, "synthetic", Effort.HIGH,
                           TokenUsage(100, 20, 15, 7), 10, 2, "accepted", 1, LimitRemaining(80, 90), None, when)

    def test_usage_rotation_dates_and_no_contents(self) -> None:
        now = datetime(2026, 11, 3, tzinfo=timezone.utc)
        log = UsageLog(self.directory)
        dates = [now, now-timedelta(days=7), now-timedelta(days=20), now+timedelta(days=1)]
        for when in dates:
            log.append(self.usage_record(when))
        records = log.read_days(14, now=now)
        self.assertEqual([record.ts for record in records], sorted(dates[:2]))
        self.assertEqual(len(list(self.directory.glob("usage-*.jsonl"))), 2)
        for path in self.directory.glob("usage-*.jsonl"):
            self.assertNotIn("Кириллица", path.read_text(encoding="utf-8"))
        self.assertEqual(records[0].usage.input_tokens, 100)

    def test_concurrent_usage_append(self) -> None:
        log = UsageLog(self.directory)
        now = datetime.now(timezone.utc)
        record = self.usage_record(now)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: log.append(record), range(80)))
        self.assertEqual(len(log.read_days(now=now+timedelta(seconds=1))), 80)

    def test_append_short_write_then_error_rolls_back(self) -> None:
        log = UsageLog(self.directory)
        now = datetime.now(timezone.utc)
        record = self.usage_record(now)
        path = log.append(record)
        original = path.read_bytes()
        real_write = os.write
        calls = 0

        def failing_write(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:7])
            raise OSError("synthetic interrupted write")

        with patch("reoengine._data.os.write", side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "synthetic interrupted write"):
                log.append(record)
        self.assertEqual(calls, 2)
        self.assertEqual(path.read_bytes(), original)
        log.append(record)
        self.assertEqual(len(read_lines(path)), 2)
        self.assertEqual(log.read_days(now=now), [record, record])
        self.assertFalse(path.with_name(path.name + ".corrupt").exists())

    def test_append_short_writes_and_zero_progress(self) -> None:
        path = self.directory / "journal.jsonl"
        real_write = os.write
        with patch("reoengine._data.os.write", side_effect=lambda fd, data: real_write(fd, data[:3])):
            self.assertFalse(append_line(path, {"synthetic": "кириллица"}))
        original = path.read_bytes()
        with patch("reoengine._data.os.write", return_value=0):
            with self.assertRaisesRegex(OSError, "no progress"):
                append_line(path, {"synthetic": "next"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(read_lines(path), [{"synthetic": "кириллица"}])

    def test_read_lines_skips_only_unterminated_last_line(self) -> None:
        path = self.directory / "journal.jsonl"
        for tail in (b'{"unfinished":', b'{"complete":true}', b'{"utf8":"\xd0'):
            with self.subTest(tail=tail):
                path.write_bytes(encode({"complete": 1}) + tail)
                self.assertEqual(read_lines(path), [{"complete": 1}])
        path.write_bytes(encode({"complete": 1}) + b'{broken}\n' + encode({"complete": 2}))
        with self.assertRaises(json.JSONDecodeError):
            read_lines(path)

    def test_recovery_preserves_completed_records_and_appends_corrupt_bytes(self) -> None:
        path = self.directory / "journal.jsonl"
        corrupt = path.with_name(path.name + ".corrupt")
        corrupt.write_bytes(b"previous fragment")
        fragments = (b"x" * 20000 + b"\xd0", b'{"unfinished":')
        for index, fragment in enumerate(fragments):
            with self.subTest(index=index):
                with path.open("ab") as stream:
                    stream.write(fragment)
                self.assertTrue(append_line(path, {"complete": index}))
                self.assertEqual(read_lines(path), [{"complete": i} for i in range(index + 1)])
                self.assertEqual(corrupt.read_bytes(), b"previous fragment" + b"".join(fragments[:index + 1]))
        self.assertFalse(append_line(path, {"complete": 2}))

    def test_archive_failure_keeps_incomplete_tail(self) -> None:
        path = self.directory / "journal.jsonl"
        original = encode({"complete": 1}) + b'{"unfinished":'
        path.write_bytes(original)
        with patch("reoengine._data.os.write", side_effect=OSError("synthetic archive failure")):
            with self.assertRaisesRegex(OSError, "synthetic archive failure"):
                append_line(path, {"complete": 2})
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(append_line(path, {"complete": 2}))
        self.assertEqual(read_lines(path), [{"complete": 1}, {"complete": 2}])

    def test_usage_recovers_after_writer_process_dies_mid_record(self) -> None:
        log = UsageLog(self.directory)
        now = datetime.now(timezone.utc)
        record = self.usage_record(now)
        path = log.append(record)
        original = path.read_bytes()
        process = subprocess.run([sys.executable, str(FAKE.parent / "usage_writer.py"),
                                  str(self.directory), "1", "crash"], capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 23, (process.stdout, process.stderr))
        interrupted = path.read_bytes()
        self.assertTrue(interrupted.startswith(original))
        fragment = interrupted[len(original):]
        self.assertTrue(fragment)
        self.assertFalse(fragment.endswith(b"\n"))
        self.assertEqual(log.read_days(now=now), [record])
        self.assertTrue(append_line(path, record.to_json()))
        self.assertEqual(path.with_name(path.name + ".corrupt").read_bytes(), fragment)
        self.assertEqual(log.read_days(now=now), [record, record])

    def test_usage_append_from_multiple_processes(self) -> None:
        writer = FAKE.parent / "usage_writer.py"
        path = self.directory / f"usage-{datetime.now(timezone.utc):%Y-%m}.jsonl"
        fragment = b'{"interrupted":'
        path.write_bytes(fragment)
        processes = [subprocess.Popen([sys.executable, str(writer), str(self.directory), "20"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(len(UsageLog(self.directory).read_days()), 80)
        self.assertEqual(path.with_name(path.name + ".corrupt").read_bytes(), fragment)


if __name__ == "__main__":
    unittest.main()
