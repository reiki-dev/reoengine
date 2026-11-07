from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import FakeAdapter, make_inputs, make_pipeline

directory = Path(sys.argv[1])
make_pipeline(directory, directory / "repo", FakeAdapter("crash")).start(make_inputs())
