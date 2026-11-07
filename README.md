# ReOEngine

**Reona Orchestrator Engine**: a controlled pipeline for AI-assisted software changes.
Every change passes deterministic gates, an independent model review, and a human approval,
and leaves a local audit trail.

> Status: early release (0.1.0). Python 3.11+, standard library only, no runtime dependencies.
> Read the [security model](docs/SECURITY_AND_COMPLIANCE.md), including its limits, before using it
> on code that matters.

## Why

Coding agents are fast and sometimes wrong. ReOEngine does not try to be a smarter agent. It wraps
the agents you already use in a process that a team can defend:

- **Deterministic gates, not model claims.** Your project's tests and type checks run in code;
  path rules (`allowed_paths`, `blocked_paths`) are checked on the diff.
- **Independent review.** A separate role, optionally a different vendor, must return a
  schema-valid verdict bound to the exact diff and gate results. High-risk or large changes are
  reviewed against the full patch.
- **Human in the loop.** The engine never commits, pushes or merges. Each run ends in
  `awaiting_human`; you `approve` or `reject`.
- **Evidence.** Per-run events, exact prompts, hashes, gate results and per-call usage and cost are
  stored locally.
- **Bring your own keys, or your own CLIs.** Per role, use a vendor CLI you are signed in to, or a
  provider API with your key in an environment variable. Mix vendors freely.
- **Bounded.** Fix loops stop on a round limit, a token budget, or lack of progress.

## What it is not

Not an autonomous developer, not a sandbox, not a compliance certification, not a hosted service.
See [known limits](docs/SECURITY_AND_COMPLIANCE.md#6-known-limits-read-before-relying-on-this).

## Install

```bash
git clone https://github.com/reiki-dev/reoengine.git
cd reoengine
python -m pip install ./engine        # installs the `reoengine` and `reoengine` commands
reoengine doctor                      # checks git, vendor CLIs and configuration
```

Or run in place, without installing: `cd engine && python -m reoengine doctor`.

You also need `git`, and whichever vendor CLIs you choose to use for roles (`codex`, `claude`,
`agy`) installed and signed in, or an API key in an environment variable such as `GEMINI_API_KEY`.

## Quick start

1. **Describe your project.** Copy `docs/example-project.toml` to
   `config/projects/<name>.local.toml` (git-ignored), set the repository path through an
   environment variable (`${ENV:NAME}` is expanded when the file is loaded), list the allowed
   paths and the check commands as argv arrays.
2. **Write a task** (`task.json`, validated against
   `engine/reoengine/_resources/schemas/task.json`): an id, title, description, numbered
   requirements, `allowed_paths`, acceptance check ids, `tdd_gate`, and a profile.
3. **Run it.**

   ```bash
   reoengine run <project> --task task.json            # architect writes the plan
   reoengine run <project> --task task.json --plan plan.json --research
   reoengine status [RUN_ID]
   ```

4. **Review the result** in `runs/RUN_ID/` (`result.json`, `review/diff.patch`, `gates.json`),
   then decide:

   ```bash
   reoengine approve RUN_ID
   reoengine reject RUN_ID --reason "..."
   ```

   Approval does not commit anything. Apply the worktree's change to your repository yourself.

Other commands: `resume RUN_ID`, `switch ROLE MODEL`, `tune`, `gc`, `doctor`.
Choose a role set with `--roles FILE`, for example
`config/profiles-roles/subscription-only.toml` or `config/profiles-roles/hybrid-gemini-api.toml`.

## Pipeline

```
preflight -> brief -> research? -> plan -> implement -> gates -> review -> accept
                                              ^           |         |
                                              +-- fix loop +--------+
```

| Stage | What happens |
|---|---|
| preflight | Clean committed tree at `base_sha`, secret-name checks, isolated `git worktree` |
| plan | Plan validated against a schema (or written by the `architect` role); allowed paths must nest: plan within task within project |
| implement | The `coder` role works in the worktree |
| gates | Path rules, project checks, and a red/green TDD check when enabled |
| review | A `reader` condenses the diff into a digest; the `reviewer` returns a verdict; high-risk changes also get the full patch |
| accept | `awaiting_human` with the diff and every check result |

## Configuration

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md). In short: `config/roles.toml` maps roles to
engines and models, `config/limits.toml` sets timeouts, fix-loop limits and isolation,
`config/pricing.toml` (optional, you fill the prices) turns API token counts into cost,
`config/profiles/*.toml` holds per-language rules, `config/projects/*.toml` describes your repositories.

## Provider terms

Subscription CLIs are for ordinary individual use by the signed-in person; use API keys for teams
and anything that serves others. Read [docs/PROVIDER_TERMS.md](docs/PROVIDER_TERMS.md).

## Development

```bash
cd engine
python -m unittest discover -s tests          # full suite
python -m mypy --strict reoengine                 # type check
```

Tests use fake CLIs and local fake HTTP servers; they need no network, accounts or keys.
Optional live smoke tests against real CLIs are not part of the default run.

## Placeholders to fill before publishing

This repository was prepared for publication with placeholders where a person's details are needed.
Search for these and replace them:

| Placeholder | Where | Fill with |
|---|---|---|
| Copyright holder `reiki-dev` | `NOTICE` | Already set to the publisher handle; change it if the holder should be a person or company |
| Private vulnerability reporting | GitHub repository settings | Enable it (Security tab); `SECURITY.md` relies on it |
| Provider prices | `config/pricing.toml` | Current prices per million tokens from your provider; ships empty |

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
