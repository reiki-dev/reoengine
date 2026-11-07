# Design notes

Why the engine is built the way it is. These are decisions, not guarantees; the guarantees and
their limits are in [SECURITY_AND_COMPLIANCE.md](SECURITY_AND_COMPLIANCE.md).

## Code decides, models propose

Every transition, gate and limit is ordinary code. A model never declares a stage "passed". The
reviewer's verdict is evidence that must be schema-valid and bound to the exact diff and gate
hashes; it is not authority. The final decision is always a human `approve`.

## Why the reviewer sees a digest (and sometimes the full patch)

Review is expensive in tokens and quota, and a reviewer that is allowed to read the repository
drifts into re-doing the work. So a separate `reader` condenses the diff into a structured digest
(checklist, suspicious excerpts with exact lines, unknowns) and the engine validates every excerpt
against the real patch. The reviewer reads only that. The cost of a digest is that a reviewer can
miss what the reader omitted, so the engine does not trust it blindly: the digest must cover every
changed file, and for high-risk or large changes the reviewer also gets the full patch. The tier
comes from configuration and the diff, never from a model.

## Why a neutral directory

`architect` and `reviewer` run in an empty directory outside any project, with no `CLAUDE.md`
style instruction files in any parent. They cannot read the repository, and a project's own
instruction files cannot steer them.

## Why a worktree, and why detection instead of prevention

Agents work in a disposable `git worktree` so a bad run never touches your checkout. Containment
beyond that depends on the vendor CLI's own sandbox. The engine adds a guard that compares
snapshots before and after each agent call. That is detection: it tells you something wrote where
it should not have, it does not stop the write. A real boundary needs a container or VM, and the
`wrapper` isolation mode exists so you can supply one for the project checks.

## Why the engine never commits

Commits and pushes are the points where mistakes become hard to undo and where accountability
matters. The engine stops at `awaiting_human`. Applying the change is a deliberate human act.

## Why vendors are per role

Mixing vendors gives an independent reviewer (a model from another vendor does not share the
coder's blind spots), lets quota or outages on one vendor fall back to another, and lets a team
keep a vendor away from the roles that see code.

## Why the standard library only

No runtime dependencies means nothing to vet, pin or update in a regulated environment, and the
engine runs wherever Python 3.11 runs. The cost is small in-house pieces such as the JSON Schema
validator (see below).

## Schema subset

`reoengine/schemas.py` validates the engine's own artifact schemas and responses. It implements a
deliberately small subset: `type`, `properties`, `required`, `additionalProperties`, `items`,
`enum`, `anyOf` and `pattern` (plus the annotations `$schema`, `title`, `description`). Any other
keyword makes the validator **fail closed** with an error instead of silently ignoring it, so a
schema never claims more than is enforced. It is not a general JSON Schema implementation (no
`$ref`, `oneOf`, numeric or length bounds). Model-facing schemas follow the strict-mode
conventions of the providers: all keys required, optional values expressed as `null`.

## Attempt arithmetic

An *attempt* is one pass of implement and gates. `attempt` starts at 1. After a failed gate or a
rejected review the fix loop starts attempt `n + 1`, until `attempt` would exceed
`max_fix_rounds + 1` (so `max_fix_rounds = 3` allows four coder invocations in total). The fix loop
also stops early on a token budget or when two consecutive attempts neither reduce the number of
failing checks nor change the diff. `resume --retry-failed` is separate: it re-runs a failed first
implement attempt at most twice, only after a transient failure (quota, timeout), from a fresh
worktree.

## Token estimates

Size limits use a rough estimate of one token per four bytes of UTF-8. It underestimates for
non-Latin scripts and for dense code. Treat `max_skill_tokens` and the digest size limit as
guard rails, not exact budgets.
