# Configuration reference

The engine reads one configuration directory (`config/` by default; override with
`--config-dir` or `REONA_CONFIG_DIR`). Loaders are strict: unknown keys are rejected.
Mutable state lives in `state/` and run data in `runs/` (`--state-dir`, `--runs-dir`,
`REONA_STATE_DIR`, `REONA_RUNS_DIR`). The engine never rewrites your configuration files.

## Variables in project files

In `config/projects/*.toml`, string values for `repo`, `links.*` and check argv elements may use
`${REONA_ROOT}` (the repository root), `${HOME}`, and `${ENV:NAME}` (an environment variable; a
missing variable is an error that names it). `repo` must be absolute after expansion.

## `roles.toml`

One table per role. Roles used by the pipeline: `researcher`, `architect`, `coder`, `reader`,
`reviewer`. Other roles in the shipped file are available for external use and are not called by the
state machine.

```toml
[roles.coder]
engine = "codex"            # codex | agy | claude | gemini | anthropic | openai
model = "gpt-6-luna"
effort = "high"             # minimal low medium high xhigh max ultra
sandbox = "workspace-write" # or read-only
fallback = []               # model names tried on quota exhaustion

[roles.reviewer]
engine = "gemini"           # HTTP API; key read from the named environment variable
model = "gemini-3.8-flash"
api_key_env = "GEMINI_API_KEY"
# base_url = "https://..."  # optional; https, or http://127.0.0.1 / localhost for tests
neutral_cwd = true          # run in an empty directory outside any project
sandbox = "read-only"
```

API engines (`gemini`, `anthropic`, `openai`) support only roles that answer with JSON
(`architect`, `reviewer`, `reader`, `writer`). Roles that edit files (`coder`, `tester`,
`researcher`) need a CLI engine. Only the Gemini request format has been checked against the
provider's documentation; `anthropic` and `openai` are experimental.

Ready-made role sets are in `config/profiles-roles/`. Select one with `--roles FILE`.

Runtime overrides (`reoengine switch ROLE MODEL`) are written to `state/overrides.json` and take
effect from the next stage. Precedence: overrides, then tuning, then `roles.toml`.

## `limits.toml`

| Key | Default | Meaning |
|---|---|---|
| `max_slots`, `[slots]` | 8; coder 1, reviewer 1, readers 4 | Concurrency limits |
| `timeout_seconds`, `[timeouts]` | 600; coder 1800, reader 1200, reviewer 1200 | A role-specific timeout wins over `timeout_seconds`; gates use `timeout_seconds` |
| `max_fix_rounds` | 3 | Fix-loop rounds after the first attempt |
| `fix_loop_token_budget` | 400000 | Stop when the run's coder tokens exceed this (0 disables) |
| `fix_loop_no_progress_stop` | true | Stop when a round neither reduces failed checks nor changes the diff |
| `review_full_diff_lines` | 400 | A diff larger than this is reviewed against the full patch |
| `max_prompt_bytes` | 2097152 | Hard cap on a prompt |
| `failure_ttl_days` | 7 | Retention of bounded failure tails under `runs/*/fail/` |
| `[quota]` | | Exit codes and stderr patterns that mean "quota exhausted" |
| `[isolation]` | `mode = "env"` | `env` filters the environment only. `wrapper` runs every check behind your argv prefix; `{worktree}` is substituted |

Example wrapper (Linux, `bubblewrap` installed; adapt and test it yourself):

```toml
[isolation]
mode = "wrapper"
wrapper = ["bwrap", "--ro-bind", "/", "/", "--bind", "{worktree}", "{worktree}", "--unshare-net", "--chdir", "{worktree}"]
```

## `pricing.toml` (optional)

Prices in USD per million tokens, per model name. It ships with everything commented out: fill it
from your provider's current price list. With a price, API calls get `cost_usd` and
`cost_source = "computed"` in the usage log; CLI-reported costs are tagged `reported`.

```toml
[gemini-3.8-flash]
input = 0.0
output = 0.0
# cached_input = 0.0      # defaults to input
# reasoning_output = 0.0  # defaults to output
```

## `profiles/*.toml`

Language or stack rules passed to the models verbatim: `rules` (text), `blocked_paths`,
`checks` (argv arrays), `skills`, `max_skills`, `max_skill_tokens`. `profiles/default.toml` is a
generic starting point.

## `projects/*.toml`

```toml
repo = "${ENV:MY_REPO}"
profile = "default"
allowed_paths = ["src/**", "tests/**"]
checks = [["python", "-m", "unittest", "discover"]]
high_risk_paths = ["src/payments/**"]       # always reviewed against the full patch
exclude_from_worktree = [".env.example"]    # exact template files only
offline_env = { APP_ENV = "test" }          # extra variables for checks

[acceptance]                                # check ids a task may select
unit = ["python", "-m", "unittest", "tests.test_one"]

[links]                                     # optional: shared directories linked into the run
node_modules = "${ENV:MY_REPO}/node_modules"
```

A `[links]` target is shared and writable from the worktree. The engine warns about this in
`result.json`. Prefer wrapper isolation that mounts it read-only.

Tasks select checks by **id** from `[acceptance]`; a model-supplied argv is never executed unless
you pass `--allow-legacy-argv-checks`.

## Exit codes

`0` success or waiting for a human; `1` stage, gate or doctor failure; `2` arguments or
configuration; `3` quota exhausted.
