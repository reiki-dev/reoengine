from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reoengine._data import atomic_write
from reoengine.cli import main
from reoengine.state import RunState, Stage, Status, save_state


class ReviewCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="b2-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.state_dir = self.root / "state"
        self.config = self.root / "config"
        self.repo = self.root / "project"
        self.run_id = "run-b2"
        self.run_dir = self.runs / self.run_id
        self.run_dir.mkdir(parents=True)
        self._repo()
        self._config()
        atomic_write(self.run_dir / "launch.json", {"project": "synthetic"})
        save_state(self.run_dir, RunState(self.run_id, Stage.ACCEPT, Status.AWAITING_HUMAN,
                                          base_sha=self._git("rev-parse", "HEAD")))
        atomic_write(self.run_dir / "task.json", {"title": "Synthetic task"})
        atomic_write(self.run_dir / "review.json", {"approved": True, "summary": "synthetic", "findings": [
            {"path": "src/value.txt", "line": 1, "severity": "low", "text": "fake ghp_abcdefghijklmnopqrstuvwxyz1234567890"}]})
        atomic_write(self.run_dir / "gates.json", {"passed": True, "checks": [
            {"name": "unit", "passed": True, "exit_code": 0}]})
        atomic_write(self.run_dir / "result.json", {"status": "awaiting_human", "base_sha": self._git("rev-parse", "HEAD"),
                                                      "head_sha": "", "usage_summary": {"input_tokens": 5},
                                                      "artifacts": ["review/diff.patch", "gates.json"]})
        (self.run_dir / "review").mkdir()
        value = self.repo / "src/value.txt"
        value.write_text("new\n", encoding="utf-8")
        patch = subprocess.run(["git", "diff", "--", "src/value.txt"], cwd=self.repo,
                               check=True, capture_output=True, text=True).stdout
        value.write_text("old\n", encoding="utf-8")
        (self.run_dir / "review/diff.patch").write_text(patch, encoding="utf-8")
        (self.run_dir / "review/review-digest.json").write_text("{}", encoding="utf-8")
        (self.run_dir / "events.jsonl").write_text('{"ts":"2026-11-07T00:00:00Z","role":"coder","engine":"codex","model":"synthetic"}\n', encoding="utf-8")
        (self.run_dir / "prompts").mkdir()
        (self.run_dir / "prompts/secret-prompt.txt").write_text("synthetic prompt", encoding="utf-8")

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def _repo(self) -> None:
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Synthetic User"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "synthetic@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src/value.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)

    def _config(self) -> None:
        (self.config / "profiles").mkdir(parents=True)
        (self.config / "projects").mkdir()
        roles = "".join(f'[roles.{role}]\nengine="codex"\nmodel="{role}-model"\n' for role in
                        ("coder", "reader", "reviewer", "architect", "researcher"))
        (self.config / "roles.toml").write_text(roles, encoding="utf-8")
        (self.config / "limits.toml").write_text("", encoding="utf-8")
        (self.config / "profiles/synthetic.toml").write_text('rules=""\n', encoding="utf-8")
        (self.config / "projects/synthetic.toml").write_text(
            f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n', encoding="utf-8")

    def _main(self, args: list[str], out: io.StringIO | None = None) -> tuple[int, str]:
        output = out or io.StringIO()
        errors = io.StringIO()
        code = main(["--config-dir", str(self.config), "--state-dir", str(self.state_dir),
                     "--runs-dir", str(self.runs), *args], out=output, err=errors)
        return code, output.getvalue() + errors.getvalue()

    def test_awaiting_human_report_and_list_show(self) -> None:
        code, output = self._main(["status", self.run_id])
        self.assertEqual(code, 0)
        self.assertIn("diff:", output)
        self.assertIn("checks:", output)
        self.assertIn("findings: low=1", output)
        self.assertIn("diffstat: 1 files, 1 insertions(+), 1 deletions(-)", output)
        code, output = self._main(["list"])
        self.assertEqual(code, 0)
        self.assertIn(self.run_id, output)
        code, output = self._main(["show", self.run_id])
        self.assertEqual(code, 0)
        self.assertIn("task: Synthetic task", output)
        self.assertIn("gate: unit pass exit=0", output)

    def test_export_redacts_findings_and_skips_prompts(self) -> None:
        target = self.root / "export"
        code, _ = self._main(["export", self.run_id, "--output", str(target)])
        self.assertEqual(code, 0)
        self.assertFalse((target / "prompts/secret-prompt.txt").exists())
        findings = (target / "findings.json").read_text(encoding="utf-8")
        self.assertNotIn("ghp_", findings)

    def test_apply_creates_one_commit_and_preserves_checkout(self) -> None:
        head = self._git("rev-parse", "HEAD")
        status = self._git("status", "--porcelain")
        code, output = self._main(["apply", self.run_id, "--to-branch", "reviewed-b2"])
        self.assertEqual(code, 0, output)
        self.assertIn("branch: reviewed-b2", output)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(self._git("status", "--porcelain"), status)
        self.assertEqual(self._git("rev-list", "--count", "reviewed-b2", "^" + head), "1")
        self.assertEqual(self._git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_apply_refuses_existing_branch_and_ineligible_state(self) -> None:
        self._git("branch", "existing-b2")
        code, _ = self._main(["apply", self.run_id, "--to-branch", "existing-b2"])
        self.assertEqual(code, 2)
        save_state(self.run_dir, RunState(self.run_id, Stage.ACCEPT, Status.STOPPED, base_sha=self._git("rev-parse", "HEAD")))
        code, _ = self._main(["apply", self.run_id, "--to-branch", "new-b2"])
        self.assertEqual(code, 2)

    def test_roles_shows_override(self) -> None:
        self.state_dir.mkdir()
        atomic_write(self.state_dir / "overrides.json", {"roles": {"coder": {"model": "override-model"}}})
        code, output = self._main(["roles"])
        self.assertEqual(code, 0)
        self.assertIn("coder: engine=codex, model=override-model", output)
        self.assertIn("model=override", output)


if __name__ == "__main__":
    unittest.main()
