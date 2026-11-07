"""Offline reader: return a schema envelope and record synthetic arguments."""
from __future__ import annotations

import json
import sys
from pathlib import Path

mode, trace_name = sys.argv[1:3]
trace = Path(trace_name)
prompt = sys.stdin.buffer.read().decode("utf-8")
data = json.JSONDecoder().raw_decode(prompt.split("DATA_JSON:\n", 1)[1])[0]["data"]
attempt = len(trace.read_text(encoding="utf-8").splitlines()) + 1 if trace.exists() else 1
with trace.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[3:], "prompt": prompt}) + "\n")
digest = {"checklist": [{"id": item["id"], "satisfied": True, "evidence": "synthetic"}
                        for item in data["task"]["requirements"]],
          "checks": data["gates"]["checks"], "suspicious": [], "unknowns": [],
          "coverage": [{"path": path, "reviewed": True, "note": "synthetic review"}
                       for path in data["changed_files"]]}
if mode == "invalid_checks":
    digest["checks"] = {"invented": "invalid type, ignored by engine"}
if mode == "bad_quote" or mode == "retry_quote" and attempt == 1:
    digest["suspicious"] = [{"path": "src/value.py", "line": 1,
                             "excerpt": "invented quotation", "reason": "synthetic"}]
if mode == "bad_schema" or mode == "retry_schema" and attempt == 1:
    del digest["checklist"]
if mode in ("duplicate_id", "conflicting_id") or mode == "retry_duplicate" and attempt == 1:
    duplicate = dict(digest["checklist"][0])
    if mode == "conflicting_id":
        duplicate.update({"satisfied": False, "evidence": "contradictory synthetic evidence"})
    digest["checklist"].append(duplicate)
if mode == "missing_id":
    digest["checklist"] = []
if mode == "extra_id":
    digest["checklist"].append({"id": "unexpected", "satisfied": True, "evidence": "synthetic"})
if mode in ("bad_line", "retry_line"):
    digest["suspicious"] = [{"path": "src/value.py", "line": 999 if mode == "bad_line" or attempt == 1 else 1,
                             "excerpt": "VALUE = 1", "reason": "synthetic"}]
if mode in ("bad_mismatched_line", "retry_mismatched_line"):
    digest["suspicious"] = [{"path": "tests/test_value.py",
                             "line": 1 if mode == "bad_mismatched_line" or attempt == 1 else 5,
                             "excerpt": "+        self.assertEqual(VALUE, 1)", "reason": "synthetic"}]
print(json.dumps({"status": "SUCCESS", "structured_output": digest,
                  "response": "This prose must be ignored.",
                  "usage": {"input_tokens": 10, "output_tokens": 3}}))
