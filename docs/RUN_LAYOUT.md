# Run and state layout

Everything the engine writes is plain files. Paths are relative to the `runs/` and `state/`
directories (`--runs-dir`, `--state-dir`, `REOENGINE_RUNS_DIR`, `REOENGINE_STATE_DIR`).

## `runs/RUN_ID/`

| Path | Written by | Content |
|---|---|---|
| `worktree/` | preflight | The isolated `git worktree` the agents and the checks work in. Detached HEAD at `base_sha` |
| `launch.json` | start | Fingerprint of how the run was started (project, task id, configuration hashes) |
| `inputs.json` | start | Task, plan and research flag, normalized worktree exclusions. `resume` refuses if they changed |
| `task.json`, `plan.json`, `research.json` | brief, plan, research | The validated task, the plan (yours or the architect's), the researcher's output |
| `state.json` | every stage | Current stage, status, attempt, `base_sha`, `head_sha`, input and output hashes, fix-loop progress markers |
| `checkpoints/STAGE-ATTEMPT.json` | every stage | The validated output of each stage and attempt; later stages read these, not memory |
| `events.jsonl` | every stage and agent call | One JSON line per event: stage start and end, agent call, role, engine, model, exit code, duration, input and output hashes, categories of failures. No prompts, no responses, no file names of protected paths |
| `prompts/STAGE-ATTEMPT-ROLE.txt` | every agent call | The exact prompt text sent |
| `gates.json` | gates | Each check: name, argv outcome class (`pass`, `assertion_failed`, `infra_error`, `timeout`, `unknown`), exit code, bounded output tails, and the isolation level used (`env-only` or `wrapper`) |
| `feedback.json` | fix loop | The sanitized, bounded failure tails and review findings handed to the coder as data |
| `review/diff.patch` | review | The diff that was reviewed |
| `review/review-digest.json` | review | The reader's structured digest, completed with the authoritative gate results |
| `review/binding.json` | review | Binds the verdict to the diff and gate hashes; records review mode (`digest` or `full_diff`), reason and risk tier |
| `review.json` | review | The reviewer's schema-valid verdict |
| `result.json` | accept and every terminal state | Status, base and head SHA, changed files, gate and review summary, usage summary, warnings, `stop_reason` when stopped |
| `error.json` | failures | Category and (when the failure is transient) a retry hint; no secrets |
| `retries.json` | `resume --retry-failed` | The retry budget for the failed implement stage |
| `fail/*.json` | failures | Bounded raw tails of rejected model responses with an `expires_at` (`failure_ttl_days`); removed by `reoengine gc` |
| `usage/` | agent calls | Per-run usage receipts |
| `run-links.json` | preflight | Manifest of linked directories (`[links]`); `gc` removes the links of finished runs |
| `run.lock` | any command | PID lock; a stale lock is reclaimed only with `resume --remove-stale` |

## `state/`

| Path | Content |
|---|---|
| `usage-YYYY-MM.jsonl` | One record per agent call: role, engine, model, effort, tokens, cost and `cost_source`, duration, outcome |
| `task-outcomes.jsonl` | One record per run that a human **approved** |
| `overrides.json` | Role overrides written by `switch`; applied from the next stage |
| `tuning.json`, `tuning-log.jsonl` | Settings written by `tune --apply` and their journal |
| `events.jsonl` | Global events such as role switches |
| `*.lock` | File locks |

## What is never written

Prompts and model responses are not in `events.jsonl`. Protected file names (matched by the secret
and data name patterns) are never recorded, only categories and counts. API keys are never written
anywhere. See [SECURITY_AND_COMPLIANCE.md](SECURITY_AND_COMPLIANCE.md).
