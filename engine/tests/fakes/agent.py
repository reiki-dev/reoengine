"""Synthetic agent and app-server; never imports or launches real agent tools."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    engine, mode = sys.argv[1:3]
    arguments = sys.argv[3:]
    if engine == "rpc" and mode in ("success", "linger", "rpc_error", "bad_window"):
        initialize = json.loads(sys.stdin.buffer.readline())
        assert initialize["method"] == "initialize"
        print(json.dumps({"id":1,"result":{}}), flush=True)
        initialized = json.loads(sys.stdin.buffer.readline())
        request = json.loads(sys.stdin.buffer.readline())
        assert initialized["method"] == "initialized"
        assert request["method"] == "account/rateLimits/read"
        if mode == "rpc_error":
            print(json.dumps({"id":2,"error":{"code":-1,"message":"synthetic"}}), flush=True)
        else:
            print(json.dumps({"id":2,"result":{"rateLimits":{"primary":{"usedPercent":25,"windowDurationMins":300},"secondary":{"usedPercent":60,"windowDurationMins":10080 if mode != "bad_window" else 42}}}}), flush=True)
        if mode == "linger":
            time.sleep(30)
        return
    if engine == "rpc":
        # Error fakes must not wait for EOF during an interactive handshake.
        sys.stdin.buffer.readline()
        if mode == "timeout":
            time.sleep(30)
        elif mode == "garbage":
            print("{broken json", flush=True)
        else:
            print("unrelated failure", file=sys.stderr)
            raise SystemExit(1)
        return
    data = sys.stdin.buffer.read()
    if mode == "timeout":
        time.sleep(30)
        return
    if mode in ("tree", "tree_parent_exit", "tree_setsid"):
        marker = Path(arguments[0])
        child_mode = "setsid_child" if mode == "tree_setsid" else "child"
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "child", child_mode, str(marker)])
        marker.with_suffix(".pid").write_text(str(child.pid), encoding="utf-8")
        if mode == "tree":
            time.sleep(30)
        if mode == "tree_setsid":
            # Wait until the child has left the process group, so the test is not racy.
            ready = marker.with_suffix(".ready")
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.05)
        return
    if mode == "child":
        time.sleep(1.5)
        Path(arguments[0]).write_text("survived", encoding="utf-8")
        return
    if mode == "setsid_child":
        os.setsid()
        Path(arguments[0]).with_suffix(".ready").write_text("1", encoding="utf-8")
        time.sleep(30)
        Path(arguments[0]).write_text("survived", encoding="utf-8")
        return
    if mode in ("quota", "quota_long_stderr"):
        print("quota exceeded: synthetic limit", file=sys.stderr)
        if mode == "quota_long_stderr":
            print("x" * 20000, file=sys.stderr)
        raise SystemExit(29)
    if mode == "stderr_only":
        print("quota exceeded", file=sys.stderr)
    if mode == "process":
        print("unrelated failure", file=sys.stderr)
        raise SystemExit(1)
    if mode == "garbage":
        print("{broken json")
        return
    if mode == "tails":
        sys.stdout.buffer.write(("я" * 20000).encode("utf-8"))
        sys.stderr.buffer.write(("ю" * 20000).encode("utf-8"))
        return
    if mode == "echo_invalid_utf8":
        sys.stdout.buffer.write(data + b"\xff")
        sys.stderr.buffer.write(data + b"\xfe")
        return
    message = {"hash":hashlib.sha256(data).hexdigest(), "size":len(data), "argv":arguments,
               "cwd":os.getcwd(), "empty_cwd": not any(Path.cwd().iterdir())}
    usage = {"input_tokens":100, "cached_input_tokens":20,"output_tokens":15,"reasoning_output_tokens":7,
             "cache_read_tokens":21,"thinking_tokens":8,"cache_read_input_tokens":22}
    if engine == "codex":
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        if mode != "missing_output":
            output.write_text(json.dumps(message, ensure_ascii=False), encoding="utf-8")
        if mode == "large":
            print(json.dumps({"type":"item.completed","item":{"text":"x" * 12000}}))
        if mode == "failed":
            print(json.dumps({"type":"turn.failed","error":{"message":"synthetic failure"}}))
        elif mode == "error":
            print(json.dumps({"type":"error","message":"synthetic failure"}))
        if mode != "missing_completed":
            print(json.dumps({"type":"turn.completed", "usage": usage}))
    else:
        if mode == "answer_quota":
            message["answer"] = "quota exceeded"
        if mode == "large":
            message["long_answer"] = "x" * 12000
        if engine == "agy" and "--json-schema" in arguments:
            print(json.dumps({"status": "SUCCESS", "structured_output": message,
                              "response": json.dumps(message), "usage": usage,
                              "duration_seconds": 0.25, "total_cost_usd": 0.01,
                              "is_error": mode == "failed"}))
            return
        print(json.dumps({"result":json.dumps(message),"usage":usage,"duration_seconds":0.25,
                          "total_cost_usd":0.01,"is_error":mode == "failed"}))


if __name__ == "__main__":
    main()
