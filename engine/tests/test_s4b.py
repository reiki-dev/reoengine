from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from reona.adapters.agy import AgyAdapter
from reona.adapters.base import ErrorKind
from reona.cli import main
from reona.config import Engine, RoleConfig
from reona.usage import TokenUsage


class RecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.configurations: list[tuple[str, str]] = []

    def reconfigure(self, *, encoding: str, errors: str) -> None:
        self.configurations.append((encoding, errors))

    def __bool__(self) -> bool:
        # An injected stream remains injected even if it is falsey.
        return False


class S4bTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ENGINE, prefix="s4b-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("synthetic prompt", encoding="utf-8")
        self.schema = self.root / "schema with spaces.json"
        self.schema.write_bytes((ENGINE / "reona/_resources/schemas/review.json").read_bytes())
        self.trace = self.root / "argv.json"

    def test_agy_structured_envelope_and_flags(self) -> None:
        role = RoleConfig(Engine.AGY, "synthetic")
        relative_schema = Path(os.path.relpath(self.schema))
        self.assertFalse(relative_schema.is_absolute())
        for mode in ("success", "bad_response", "missing_response"):
            with self.subTest(mode=mode):
                command = (sys.executable, str(ENGINE / "tests/fakes/agy_schema.py"), mode, str(self.trace))
                result = AgyAdapter(command, capabilities=("--sandbox", "--mode")).invoke(
                    role, self.prompt, self.root, 5, relative_schema)
                self.assertTrue(result.success, result)
                assert result.final_message_path is not None
                self.assertEqual(json.loads(result.final_message_path.read_text(encoding="utf-8")),
                                 {"approved": True, "summary": "synthetic", "findings": []})
                self.assertEqual(result.usage, TokenUsage(100, 21, 15, 8, 0.01, 0.25, "reported"))
                argv = json.loads(self.trace.read_text(encoding="utf-8"))
                self.assertEqual(argv[argv.index("--json-schema") + 1], str(self.schema.resolve()))
                self.assertEqual(argv[argv.index("--output-format") + 1], "json")
                self.assertNotIn("--print", argv)
        # Without a schema the same adapter continues to accept the legacy response.
        for mode in ("success", "failed_status", "invalid_structured"):
            with self.subTest(without_schema=mode):
                command = (sys.executable, str(ENGINE / "tests/fakes/agy_schema.py"), mode, str(self.trace))
                result = AgyAdapter(command, capabilities=("--sandbox", "--mode")).invoke(
                    role, self.prompt, self.root, 5)
                self.assertTrue(result.success, result)
                assert result.final_message_path is not None
                self.assertEqual(json.loads(result.final_message_path.read_text(encoding="utf-8")),
                                 {"approved": True, "summary": "synthetic", "findings": []})
                self.assertNotIn("--json-schema", json.loads(self.trace.read_text(encoding="utf-8")))

    def test_agy_rejects_invalid_envelopes_and_schema(self) -> None:
        for mode, expected in (("missing_structured", ErrorKind.SCHEMA),
                               ("nonobject_structured", ErrorKind.SCHEMA),
                               ("invalid_structured", ErrorKind.SCHEMA),
                               ("failed_status", ErrorKind.PROTOCOL),
                               ("missing_status", ErrorKind.PROTOCOL)):
            with self.subTest(mode=mode):
                command = (sys.executable, str(ENGINE / "tests/fakes/agy_schema.py"), mode, str(self.trace))
                result = AgyAdapter(command, capabilities=("--sandbox", "--mode")).invoke(
                    RoleConfig(Engine.AGY, "synthetic"), self.prompt, self.root, 5, self.schema)
                self.assertFalse(result.success)
                self.assertEqual(result.error, expected)
                self.assertEqual(result.usage.input_tokens, 100)

    def gc_args(self) -> list[str]:
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        (config / "roles.toml").write_text('[roles.coder]\nengine="agy"\nmodel="synthetic"\n', encoding="utf-8")
        (config / "limits.toml").write_text("max_fix_rounds=2\n", encoding="utf-8")
        (config / "profiles").mkdir(exist_ok=True)
        (config / "projects").mkdir(exist_ok=True)
        return ["--runs-dir", str(self.root / "runs"), "--state-dir", str(self.root / "state"),
                "--config-dir", str(config), "gc"]

    def test_cli_module_redirected_output_is_utf8(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        process = subprocess.run([sys.executable, "-m", "reona", *self.gc_args()], cwd=ENGINE,
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        self.assertIn("Removed", process.stdout.decode("utf-8"))

    def test_cli_repairs_cp1252_streams(self) -> None:
        script = ("import sys; from reona.cli import main; "
                  "sys.stdout.reconfigure(encoding='cp1252'); sys.stderr.reconfigure(encoding='cp1252'); "
                  "raise SystemExit(main())")
        process = subprocess.run([sys.executable, "-c", script, *self.gc_args()], cwd=ENGINE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        self.assertIn("Removed", process.stdout.decode("utf-8"))

    def test_cli_injected_streams_are_not_reconfigured(self) -> None:
        out, err = RecordingStream(), RecordingStream()
        standard_out, standard_err = RecordingStream(), RecordingStream()
        with patch("sys.stdout", standard_out), patch("sys.stderr", standard_err):
            self.assertEqual(main(self.gc_args(), out=out, err=err), 0)
        self.assertIn("Removed", out.getvalue())
        for stream in (out, err, standard_out, standard_err):
            self.assertEqual(stream.configurations, [])

    def test_cli_configures_each_noninjected_stream(self) -> None:
        for inject_output in (False, True):
            with self.subTest(inject_output=inject_output):
                out, err = RecordingStream(), RecordingStream()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    self.assertEqual(main(self.gc_args(), out=out if inject_output else None,
                                          err=None if inject_output else err), 0)
                self.assertEqual(out.configurations, [] if inject_output else [("utf-8", "replace")])
                self.assertEqual(err.configurations, [("utf-8", "replace")] if inject_output else [])


if __name__ == "__main__":
    unittest.main()
