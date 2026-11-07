# Security and regulatory safeguards model

This document states what ReOEngine does and does not guarantee. It is written for the person who
has to answer an auditor's question: *who wrote this change, who reviewed it, what did the models
see, and what is recorded?*

It describes the behavior of the code in this repository. Where a protection is a **policy or a
detection** rather than a **prevention**, it says so. Read section 6 before relying on any claim.

## 1. What ReOEngine is

A deterministic pipeline around AI coding agents. Models propose; deterministic code decides.

```
task + plan -> [worktree] -> coder -> [gates] -> digest -> reviewer -> awaiting_human
                                        ^   fix loop  |                     |
                                        +-------------+          approve / reject
```

- The **state machine** (preflight, research, plan, implement, gates, review, fix loop, accept)
  and every transition are ordinary code. No model decides whether a stage passed.
- **Gates** run your project's checks (tests, type checks) and path rules. A model's claim that
  "tests pass" is never used.
- **The review is a second, independent model call** (a different role, optionally a different
  vendor) that must return a schema-valid verdict.
- **A human finishes every run.** The engine never commits, pushes or merges. A run ends in
  `awaiting_human`; `reoengine approve RUN_ID` or `reoengine reject RUN_ID --reason ...` is the
  only way forward.

## 2. What each model can see

| Role | Runs in | Sees |
|---|---|---|
| `coder` (and `tester`, `researcher`) | An isolated git worktree of the target repository | The files in that worktree, the task, the plan, project rules, sanitized gate feedback |
| `reader` (digest) | The run directory | The full diff, the task and the gate results; it writes a structured digest |
| `reviewer` | An empty "neutral" directory outside any project | The digest only. For high-risk changes (see 2.1) also the full diff. It reads no files |
| `architect` | The same empty neutral directory | The task and supplied context only |

Files you list in `exclude_from_worktree` are absent from the worktree on disk. Names that match
the secret/data name patterns (`.env*`, `*.db`, `*.pem`, `*.key`, ...) are blocked by the gates and
refused at preflight when they are tracked. **This is a policy check, not a sandbox**: see 6.

### 2.1 Review tiers

The review tier is computed by the engine from the diff and the configuration. A model cannot lower it.
A change is **high risk** when it touches a built-in sensitive path (adapters, gates, guard, auth,
migrations, CI and supply-chain files), a path matched by the project's `high_risk_paths`, when
the TDD gate is disabled, or when the diff changes more than `review_full_diff_lines` lines
(default 400). For high-risk changes the reviewer receives the digest **and the full patch**, the
digest must cover every changed file, and unresolved unknowns block approval. The mode and reason
(`digest` or `full_diff`) are written to `review/binding.json` and the event log.

## 3. What is recorded

Everything below is written locally, under `runs/RUN_ID/` and `state/`. Nothing is sent to any
service by ReOEngine itself; ReOEngine has no telemetry and no phone-home.

| Record | Content |
|---|---|
| `events.jsonl` | One event per stage and per agent call: role, engine, model, exit code, duration, hashes of input and output. Prompts and responses are not in the event log |
| `prompts/` | The exact prompt sent for each stage and attempt |
| `state.json`, `checkpoints/`, `inputs.json` | Stage state, content hashes of inputs and outputs; resume refuses if inputs changed |
| `gates.json` | Each check: argv, exit code, classified outcome (`pass`, `assertion_failed`, `infra_error`, `timeout`, `unknown`), bounded sanitized output tail, and the isolation level used |
| `review/diff.patch`, `review/review-digest.json`, `review.json`, `review/binding.json` | The reviewed diff, the digest, the verdict, and the binding of verdict to the exact diff and gate hashes |
| `result.json` | Status, base and head SHA, changed files, gate and review summary, warnings (for example "checks ran with environment filtering only"), stop reason |
| `state/usage-YYYY-MM.jsonl` | One record per agent call: tokens, cost (with `cost_source`: reported by the CLI, computed from your `pricing.toml`, or unknown) |
| `state/task-outcomes.jsonl` | Written only when a human runs `approve` |

The event log only grows during an agent call (the guard compares the old prefix before and
after). It is **not signed and not tamper-evident** against someone with write access to the host:
for evidentiary use, ship these files to write-once storage.

## 4. What leaves the machine

Only what you configure. For each role, the prompt (task, plan, rules, diff or digest, bounded
check output) goes to the vendor of that role's engine:

- **Subscription CLI engines** (`codex`, `claude`, `agy`): the vendor CLI sends it under its own
  terms. ReOEngine does not touch the CLI's credentials.
- **API engines** (`gemini`, `anthropic`, `openai`): ReOEngine sends an HTTPS request with a key
  read from the environment variable named in the role (`api_key_env`). The key is read only at
  call time, never logged or written, and removed from any response text. Redirects are refused so
  the key cannot be forwarded to another host. `base_url` must be `https://` (or loopback for tests).

To keep a vendor away from your code, do not assign it a role that sees code. Choose roles per
vendor in `config/roles.toml`.

## 5. Controls, and how they fail

| Control | What it does | How it can fail |
|---|---|---|
| Worktree isolation | Agents work in a disposable `git worktree`, detached from branches | An agent with shell access can still read anything its OS user can read |
| Outside-write guard | Before and after every agent call, snapshots engine state, config, source repository status and git metadata, and the engine state directory; any difference fails the stage with `outside_write:<category>` | Detection, not prevention. Writes elsewhere on disk are not seen. A write that restores the previous metadata between snapshots is not seen |
| Path rules | `allowed_paths` (task within plan within project) and `blocked_paths` are checked on the diff | Applies to the diff, not to what an agent reads |
| Secret exclusion | `exclude_from_worktree` removes template files from the worktree via sparse checkout; secret-named files stop a run at preflight | The worktree shares the git object store: the content can still be read with `git show`. Absence from disk is not a confidentiality boundary |
| Check isolation | Project checks run with a filtered environment and stubbed proxies. With `[isolation] mode = "wrapper"` they run behind a command prefix you supply (for example `bwrap`) | Default `env` mode is **not** a network or filesystem sandbox. `result.json` says so (`isolation: env-only`) |
| Feedback sanitizer | Check output returned to the coder is sanitized before truncation: URI credentials, common token formats, private-key blocks, secret-word lines | Regex based. Unknown secret formats can pass |
| Independent review | A separate role (optionally a separate vendor) must return a schema-valid verdict bound to the diff and gate hashes | Models can be wrong. The review is evidence, not proof |
| Human gate | The only way to finish a run | The engine cannot stop a human from approving carelessly |
| Fix-loop limits | At most `max_fix_rounds` rounds, a token budget, and a stop when two rounds make no progress; the reason is in `result.json` | Limits cost and loops, not correctness |
| Process cleanup | On timeout the whole process tree is killed (process groups, descendant walk, environment token on Linux, Job Objects on Windows) | A process that escapes via an unusual mechanism may survive; the engine reports `kill_tree_incomplete` when it can tell |

## 6. Known limits (read before relying on this)

1. **There is no OS-level sandbox for agents.** Containment relies on the vendor CLI's own sandbox
   (for example Codex `workspace-write`) and on detection after the fact. For untrusted code or
   hostile prompts, run ReOEngine inside a container or VM you control.
2. **`env-only` check isolation does not block the network or the filesystem.** Use wrapper mode
   with a real sandbox for untrusted project checks.
3. **Detection can miss** writes outside the monitored categories and metadata-preserving rewrites.
4. **The audit trail is local and unsigned.** It records what happened to the extent the host was
   not tampered with.
5. **Secrets handling is name- and pattern-based.** It does not detect a secret stored in an
   ordinarily named file, and the sanitizer does not recognize every secret format.
6. **Model output is untrusted.** The engine treats it as data, validates schemas and re-checks
   paths, but a reviewer model can approve a bad change; the human gate exists for that reason.
7. **API adapters other than Gemini are experimental.** Their request formats follow public
   documentation as understood at the time of writing and have only been tested against local fake
   servers.
8. **Subscription mode is for individual use.** See `docs/PROVIDER_TERMS.md`.
9. **No certification.** ReOEngine is not SOC 2, ISO 27001, PCI DSS or DORA certified, and
   using it does not make you compliant. It produces evidence that can support your own controls.

## 7. Suggested deployment for regulated teams

- API mode with keys owned by your organization (`config/profiles-roles/hybrid-gemini-api.toml` is a
  starting point; assign roles per vendor under your own data agreements).
- Run inside a container or VM; set `[isolation] mode = "wrapper"` with a sandbox wrapper.
- Keep `exclude_from_worktree` and `blocked_paths` strict; keep real data out of the repository.
- Copy `runs/` and `state/` to write-once storage after each run.
- Require `reoengine approve` by a named reviewer; keep your own record of who approved.
