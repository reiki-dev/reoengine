from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reoengine.cli import main
from reoengine.events import Event, EventKind, EventLog, verify_chain


class EventChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="h1-chain-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "runs" / "run-h1"
        self.run.mkdir(parents=True)
        self.log = EventLog(self.run)

    def append(self, log: EventLog | None = None) -> None:
        (log or self.log).append(Event("run-h1", EventKind.STAGE_START))

    def lines(self) -> list[bytes]:
        return (self.run / "events.jsonl").read_bytes().splitlines()

    def test_three_events_verify(self) -> None:
        for _ in range(3):
            self.append()
        self.assertEqual([json.loads(line)["seq"] for line in self.lines()], [0, 1, 2])
        self.assertTrue(verify_chain(self.log.path).ok)

    def test_edit_delete_and_swap_are_detected(self) -> None:
        for mutation in ("edit", "delete", "swap"):
            with self.subTest(mutation=mutation):
                self.log.path.unlink(missing_ok=True)
                for _ in range(3):
                    self.append()
                lines = self.lines()
                if mutation == "edit":
                    item = json.loads(lines[1])
                    item["event"] = "stage_end"
                    lines[1] = json.dumps(item, separators=(",", ":")).encode()
                elif mutation == "delete":
                    del lines[1]
                else:
                    lines[1], lines[2] = lines[2], lines[1]
                self.log.path.write_bytes(b"\n".join(lines) + b"\n")
                result = verify_chain(self.log.path)
                self.assertFalse(result.ok)
                self.assertEqual(result.index, 1 if mutation == "edit" else 1)

    def test_partial_tail_reports_break(self) -> None:
        self.append()
        with self.log.path.open("ab") as stream:
            stream.write(b'{"partial":')
        result = verify_chain(self.log.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.index, 1)

    def test_append_recovers_partial_tail_and_keeps_chain(self) -> None:
        self.append()
        with self.log.path.open("ab") as stream:
            stream.write(b'{"partial":')
        self.append()
        self.assertTrue(verify_chain(self.log.path).ok)
        self.assertEqual([json.loads(line)["seq"] for line in self.lines()], [0, 1])

    def test_legacy_prefix_and_legacy_after_chain(self) -> None:
        self.log.path.write_bytes(b'{"event":"old"}\n')
        self.append()
        self.append()
        self.assertTrue(verify_chain(self.log.path).ok)
        with self.log.path.open("ab") as stream:
            stream.write(b'{"event":"late legacy"}\n')
        result = verify_chain(self.log.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.index, 3)

    def test_interleaved_instances(self) -> None:
        second = EventLog(self.run)
        for _ in range(3):
            self.append()
            self.append(second)
        self.assertTrue(verify_chain(self.log.path).ok)

    def test_verify_cli_exit_codes_and_doctor_warning(self) -> None:
        self.log.path.touch()
        out = io.StringIO()
        code = main(["--runs-dir", str(self.root / "runs"), "verify", "run-h1"], out=out)
        self.assertEqual(code, 0)
        self.assertIn("ok 0 events", out.getvalue())
        self.append()
        self.append()
        content = self.log.path.read_bytes().replace(b"stage_start", b"stage_end")
        self.log.path.write_bytes(content)
        out = io.StringIO()
        code = main(["--runs-dir", str(self.root / "runs"), "verify", "run-h1"], out=out)
        self.assertEqual(code, 1)
        self.assertIn("break at event 0", out.getvalue())
        out = io.StringIO()
        with patch("reoengine.cli.diagnose", return_value=[]):
            code = main(["--runs-dir", str(self.root / "runs"), "doctor"], out=out)
        self.assertEqual(code, 0)
        self.assertIn("warning: event chain: run-h1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
