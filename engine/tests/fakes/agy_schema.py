"""Synthetic agy schema envelope; never invokes an agent."""
from __future__ import annotations

import json
import sys
from pathlib import Path


mode, trace = sys.argv[1:3]
Path(trace).write_text(json.dumps(sys.argv[3:]), encoding="utf-8")
assert sys.stdin.buffer.read() == b"synthetic prompt"
review = {"approved": True, "summary": "synthetic", "findings": []}
envelope: dict[str, object] = {
    "status": "SUCCESS", "response": json.dumps(review), "structured_output": review,
    "usage": {"input_tokens": 100, "cache_read_tokens": 21, "output_tokens": 15, "thinking_tokens": 8},
    "duration_seconds": 0.25, "total_cost_usd": 0.01,
}
if mode == "bad_response":
    envelope["response"] = "Model refusal; this is prose, not JSON."
    envelope["result"] = {"invalid": "must also be ignored"}
elif mode == "missing_response":
    del envelope["response"]
elif mode == "missing_structured":
    del envelope["structured_output"]
elif mode == "nonobject_structured":
    envelope["structured_output"] = json.dumps(review)
elif mode == "invalid_structured":
    envelope["structured_output"] = {"approved": "invalid"}
elif mode == "failed_status":
    envelope["status"] = "FAILED"
elif mode == "missing_status":
    del envelope["status"]
print(json.dumps(envelope))
