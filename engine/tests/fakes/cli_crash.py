from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import FakeAdapter
from reona.cli import main
from reona.config import Engine

root = Path(sys.argv[1])
raise SystemExit(main(["run", "synthetic", "--task", str(root / "task.json"), "--plan", str(root / "plan.json")],
                      root=root, adapters={Engine.CODEX: FakeAdapter("crash")}))
