"""Read-only review commands and isolated application of reviewed changes."""
from __future__ import annotations

import json
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import TextIO

from ._data import Json, loads, read_lines
from .config import Configuration, effective_roles
from .gates import sanitize_output
from .gitops import Git
from .state import RunState, Status, load_state


def _read(directory: Path, name: str) -> Json | None:
    path = directory / name
    if not path.is_file():
        return None
    return loads(path.read_text(encoding="utf-8"))


def findings(directory: Path) -> list[dict[str, Json]]:
    value = _read(directory, "review.json")
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        return []
    raw_findings = value.get("findings")
    return [item for item in raw_findings if isinstance(item, dict)] if isinstance(raw_findings, list) else []


def diffstat(directory: Path) -> tuple[int, int, int]:
    path = directory / "review/diff.patch"
    if not path.is_file():
        return 0, 0, 0
    files = insertions = deletions = 0
    in_hunk = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+") and not line.startswith("+++"):
            insertions += 1
        elif in_hunk and line.startswith("-") and not line.startswith("---"):
            deletions += 1
        elif in_hunk and not line.startswith((" ", "\\", "+", "-")):
            in_hunk = False
    return files, insertions, deletions


def print_review(directory: Path, out: TextIO) -> None:
    review_path = directory / "review.json"
    review_data = _read(directory, "review.json") if review_path.is_file() else None
    review_findings = review_data.get("findings") if isinstance(review_data, dict) else None
    if isinstance(review_findings, list):
        items = [item for item in review_findings if isinstance(item, dict)]
        counts = Counter(str(item.get("severity", "unknown")) for item in items)
        summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "0"
        print("findings: " + summary, file=out)
        for item in items[:10]:
            severity = _clean(str(item.get("severity", "unknown")))
            path = _clean(str(item.get("path", "")))
            line = item.get("line")
            location = path + (f":{line}" if isinstance(line, int) and not isinstance(line, bool) else "")
            message = _clean(str(item.get("text", "")))
            print(f"  {severity} {location} {message[:160]}", file=out)
    patch = directory / "review/diff.patch"
    if patch.is_file():
        files, added, removed = diffstat(directory)
        print(f"diffstat: {files} files, {added} insertions(+), {removed} deletions(-)", file=out)


def _clean(value: str) -> str:
    return "".join(char for char in value if char in "\t " or unicodedata.category(char) not in {"Cc", "Cf"})


def list_runs(runs: Path, out: TextIO, status: str | None, limit: int) -> int:
    entries: list[tuple[str, str, str, str, str, RunState]] = []
    for path in runs.glob("*/state.json"):
        directory = path.parent
        state = load_state(directory)
        if status is not None and state.status.value != status:
            continue
        launch = _read(directory, "launch.json")
        project = launch.get("project", "") if isinstance(launch, dict) else ""
        created = ""
        events_path = directory / "events.jsonl"
        if events_path.is_file():
            events = read_lines(events_path)
            if events and isinstance(events[0], dict):
                created = str(events[0].get("ts", ""))
        entries.append((created, state.run_id, str(project), state.status.value, state.stage.value, state))
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    if not entries:
        print("no runs", file=out)
        return 0
    print("run_id\tproject\tstatus\tstage\tcreated", file=out)
    for created, run_id, project, current, stage, _ in entries[:limit]:
        print(f"{run_id}\t{project}\t{current}\t{stage}\t{created}", file=out)
    return 0


def show_run(state: RunState, directory: Path, out: TextIO) -> int:
    print(f"{state.run_id}: status={state.status.value}; stage={state.stage.value}; attempt={state.attempt}", file=out)
    task = _read(directory, "task.json")
    if isinstance(task, dict):
        print(f"task: {task.get('title', '')}", file=out)
    events_path = directory / "events.jsonl"
    if events_path.is_file():
        roles: set[tuple[str, str, str, str]] = set()
        for event in read_lines(events_path):
            if isinstance(event, dict) and all(isinstance(event.get(key), str) for key in ("role", "engine", "model")):
                roles.add((str(event.get("stage", "unknown")), str(event["role"]),
                           str(event["engine"]), str(event["model"])))
        for stage, role, engine, model in sorted(roles):
            print(f"role: stage={stage} {role} {engine}/{model}", file=out)
    gates = _read(directory, "gates.json")
    checks = gates.get("checks") if isinstance(gates, dict) else None
    if isinstance(checks, list):
        for raw in checks:
            if isinstance(raw, dict):
                print(f"gate: {raw.get('name', '')} {'pass' if raw.get('passed') is True else 'fail'} exit={raw.get('exit_code', '')}", file=out)
    print_review(directory, out)
    result = _read(directory, "result.json")
    if isinstance(result, dict):
        usage = result.get("usage_summary")
        if isinstance(usage, dict):
            print("usage: " + ", ".join(f"{key}={value}" for key, value in usage.items()), file=out)
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, str):
                    print(f"artifact: {artifact}", file=out)
    for candidate in ("events.jsonl", "gates.json", "result.json", "review.json", "review/review-digest.json", "review/diff.patch"):
        if (directory / candidate).is_file() and candidate not in str(result.get("artifacts", []) if isinstance(result, dict) else []):
            print(f"artifact: {candidate}", file=out)
    return 0


def export_run(directory: Path, run_id: str, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError("export destination is not empty")
    output.mkdir(parents=True, exist_ok=True)
    for relative in ("review/diff.patch", "review/review-digest.json", "gates.json", "result.json", "events.jsonl"):
        source = directory / relative
        if source.is_file():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            text = sanitize_output(source.read_text(encoding="utf-8", errors="replace"))
            target.write_text(text, encoding="utf-8")
    (output / "findings.json").write_text(
        sanitize_output(json.dumps(findings(directory), ensure_ascii=True)), encoding="utf-8")
    print(f"exported {run_id} to {output}")


def apply_run(directory: Path, state: RunState, repo: Path, branch: str, out: TextIO) -> None:
    eligible = ((state.status == Status.AWAITING_HUMAN and state.stage.value == "accept")
                or (state.status == Status.COMPLETED and state.stage.value == "done"))
    if not eligible:
        raise ValueError("run must be awaiting_human or completed")
    git = Git(repo)
    if (not branch or not branch[0].isalnum() or any(not (char.isalnum() or char in "_.-") for char in branch)
            or ".." in branch or branch.endswith(".") or branch.casefold().endswith(".lock")):
        raise ValueError("invalid branch name")
    base = state.base_sha
    if not base:
        result = _read(directory, "result.json")
        base = str(result.get("base_sha", "")) if isinstance(result, dict) else ""
    if not base:
        raise ValueError("run has no base commit")
    git.command(("check-ref-format", "--branch", branch))
    existing = git.command(("branch", "--list", branch))
    if existing:
        raise ValueError("branch already exists")
    name = git._local_config("user.name")
    email = git._local_config("user.email")
    if not name or not email:
        raise ValueError("repository git config must set user.name and user.email")
    patch = (directory / "review/diff.patch").read_bytes()
    if not patch:
        raise ValueError("review diff is missing or empty")
    patch = patch.replace(b"\r\n", b"\n")
    with tempfile.TemporaryDirectory(prefix="reoengine-apply-") as temporary:
        tree = Path(temporary) / "worktree"
        git.command(("worktree", "add", "-b", branch, str(tree), base))
        try:
            work = Git(tree)
            work.command(("apply", "--index", "--binary", "-"), stdin=patch)
            work.command(("commit", "-m", f"Apply ReOEngine run {state.run_id}"),
                         environment={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                                      "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
            commit = work.head()
        except BaseException:
            git.command(("worktree", "remove", "--force", str(tree)), allowed_exit_codes=(0, 1))
            git.command(("branch", "-D", branch), allowed_exit_codes=(0, 1))
            raise
        git.command(("worktree", "remove", "--force", str(tree)))
    print(f"branch: {branch}\ncommit: {commit}", file=out)


def roles_output(configuration: Configuration, state_dir: Path, out: TextIO) -> None:
    base = configuration.roles
    tuned = effective_roles(base, state_dir / "tuning.json")
    final = effective_roles(tuned, overrides=state_dir / "overrides.json")
    tuning_data = _read(state_dir, "tuning.json")
    override_data = _read(state_dir, "overrides.json")
    tuning_roles = tuning_data.get("roles") if isinstance(tuning_data, dict) else None
    override_roles = override_data.get("roles") if isinstance(override_data, dict) else None
    for name, role in sorted(final.items()):
        sources = []
        original = base[name]
        middle = tuned[name]
        for field, initial, tuned_value, final_value in (
                ("engine", original.engine.value, middle.engine.value, role.engine.value),
                ("model", original.model, middle.model, role.model),
                ("effort", original.effort.value, middle.effort.value, role.effort.value)):
            over = override_roles.get(name) if isinstance(override_roles, dict) else None
            tune = tuning_roles.get(name) if isinstance(tuning_roles, dict) else None
            source = ("override" if isinstance(over, dict) and field in over else
                      "tuning" if isinstance(tune, dict) and field in tune else
                      "override" if final_value != tuned_value else
                      "tuning" if tuned_value != initial else "roles file")
            sources.append(f"{field}={source}")
        print(f"{name}: engine={role.engine.value}, model={role.model}, effort={role.effort.value} ({', '.join(sources)})", file=out)

