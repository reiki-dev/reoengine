"""Create and run the offline hello-world pipeline example."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


EXAMPLE = Path(__file__).resolve().parent
ROOT = EXAMPLE.parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from reoengine.adapters.codex import CodexAdapter  # noqa: E402
from reoengine.cli import main as cli_main  # noqa: E402
from reoengine.config import Engine  # noqa: E402
from reoengine.state import load_state  # noqa: E402


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run(("git", "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
                    "-c", "commit.gpgsign=false", *arguments), cwd=directory, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)


def run_demo(directory: Path) -> str:
    """Build an isolated sample project, run the real CLI, and return its final status."""
    demo = directory.resolve()
    demo.mkdir(parents=True, exist_ok=True)
    repository = demo / "sample-repo"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "greeting.py").write_text(
        'def greeting():\n    return "Hello"\n', encoding="utf-8")
    (repository / "tests" / "test_greeting.py").write_text(
        "import unittest\nfrom src.greeting import greeting\n\n"
        "class GreetingTest(unittest.TestCase):\n"
        "    def test_greeting(self):\n"
        '        self.assertEqual(greeting(), "Hello, world!")\n', encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "Create sample project")

    config = demo / "config"
    (config / "profiles").mkdir(parents=True)
    (config / "projects").mkdir()
    shutil.copyfile(ROOT / "config" / "limits.toml", config / "limits.toml")
    shutil.copyfile(ROOT / "config" / "profiles" / "default.toml", config / "profiles" / "default.toml")
    roles = "\n".join(
        f"[roles.{role}]\nengine = \"codex\"\nmodel = \"{role}\"\n"
        f"effort = \"low\"\nsandbox = \"{'workspace-write' if role == 'coder' else 'read-only'}\"\n"
        for role in ("coder", "reader", "reviewer"))
    (config / "roles.toml").write_text(roles, encoding="utf-8")
    shutil.copyfile(EXAMPLE / "project.toml", config / "projects" / "hello.toml")
    runs = demo / "runs"
    state = demo / "state"
    old_repo = os.environ.get("EXAMPLE_REPO")
    old_python = os.environ.get("EXAMPLE_PYTHON")
    os.environ["EXAMPLE_REPO"] = str(repository)
    os.environ["EXAMPLE_PYTHON"] = sys.executable
    try:
        code = cli_main([
            "--config-dir", str(config), "--state-dir", str(state), "--runs-dir", str(runs),
            "run", "hello", "--task", str(EXAMPLE / "task.json"),
            "--plan", str(EXAMPLE / "plan.json"),
        ], adapters={Engine.CODEX: CodexAdapter(command=(sys.executable, str(EXAMPLE / "fake_agent.py")))})
    finally:
        if old_repo is None:
            os.environ.pop("EXAMPLE_REPO", None)
        else:
            os.environ["EXAMPLE_REPO"] = old_repo
        if old_python is None:
            os.environ.pop("EXAMPLE_PYTHON", None)
        else:
            os.environ["EXAMPLE_PYTHON"] = old_python
    state_files = sorted(runs.glob("*/state.json"))
    if code != 0 or not state_files:
        raise RuntimeError(f"reoengine run failed with exit code {code}")
    return load_state(state_files[-1].parent).status.value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        help="Keep all sample files and run artifacts in this directory")
    args = parser.parse_args()
    if args.directory is None:
        import tempfile
        directory = Path(tempfile.mkdtemp(prefix="reoengine-hello-world-"))
    else:
        directory = args.directory
    status = run_demo(directory)
    print(f"end_state={status}")
    print(f"demo_directory={directory.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
