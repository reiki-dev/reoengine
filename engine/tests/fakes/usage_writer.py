from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reoengine.config import Effort, Engine
from reoengine.state import Stage
from reoengine.usage import TokenUsage, UsageLog, UsageRecord


def main() -> None:
    directory = Path(sys.argv[1])
    count = int(sys.argv[2])
    if sys.argv[3:] == ["crash"]:
        real_write = os.write

        def crash_write(fd: int, data: bytes) -> int:
            real_write(fd, data[:len(data) // 2])
            os._exit(23)

        # Exit while append_line holds the OS lock, bypassing rollback/finally.
        os.write = crash_write
    log = UsageLog(directory)
    for _ in range(count):
        log.append(UsageRecord("synthetic", Stage.IMPLEMENT, "coder", Engine.CODEX,
                               "synthetic-model", Effort.HIGH, TokenUsage(100, 10, 20, 5),
                               10, 0, "accepted", 1))


if __name__ == "__main__":
    main()
