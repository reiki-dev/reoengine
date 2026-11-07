# Hello world demo

Run this from the repository root. It uses only Python's standard library and Git; no API key or real model is needed.

```text
python examples/hello-world/setup.py
```

Expected final output includes `end_state=awaiting_human`. The script prints the directory containing the temporary sample repository, configuration, run, and state. Use `--directory PATH` to choose and keep that location:

```text
python examples/hello-world/setup.py --directory hello-world-run
```

The fake Codex command is passed to the real `CodexAdapter`, then the script invokes the real `reoengine run` CLI path with the included task and plan. Look at `runs/<run-id>/review/diff.patch`, `runs/<run-id>/gates.json`, and `runs/<run-id>/result.json` to inspect the proposed change and its checks. The engine leaves it awaiting human review; it does not apply the worktree change to the sample repository.
