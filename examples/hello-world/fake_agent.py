"""Small Codex command stub for the offline hello-world example."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    arguments = sys.argv[1:]
    prompt = sys.stdin.read()
    envelope = json.loads(prompt.split("DATA_JSON:\n", 1)[1].split("\n", 1)[0])
    data = envelope["data"]
    role = Path(arguments[arguments.index("--output-last-message") + 1])
    output = role.resolve()
    prompt_role = arguments[arguments.index("--model") + 1]
    if prompt_role == "reader":
        changed = data["changed_files"]
        task = data["task"]
        response = {
            "coverage": [{"path": path, "reviewed": True, "note": "Reviewed the sample change."}
                         for path in changed],
            "checklist": [{"id": requirement["id"], "satisfied": True,
                           "evidence": "The unit-tests check passed."}
                          for requirement in task["requirements"]],
            "suspicious": [], "checks": [], "unknowns": [],
        }
    elif prompt_role == "reviewer":
        response = {"approved": True, "summary": "The sample change is correct.", "findings": []}
    else:
        (Path.cwd() / "src" / "greeting.py").write_text(
            'def greeting():\n    return "Hello, world!"\n', encoding="utf-8")
        (Path.cwd() / "tests" / "test_greeting.py").write_text(
            "import unittest\nfrom src.greeting import greeting\n\n"
            "class GreetingTest(unittest.TestCase):\n"
            "    def test_greeting(self):\n"
            '        self.assertEqual("Hello, world!", greeting())\n', encoding="utf-8")
        response = {"ok": True}
    output.write_text(json.dumps(response), encoding="utf-8")
    usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
             "reasoning_output_tokens": 0}
    print(json.dumps({"type": "turn.completed", "usage": usage}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
