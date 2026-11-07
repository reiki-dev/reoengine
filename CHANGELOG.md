# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- CLI review commands: `list`, `show`, `export`, `apply`, and `roles`, with findings and diffstat at human review.
- Complete example project, task, and plan; offline hello-world demo; Python and Node profiles.
- `tune` command (the former `preflight`, which remains as a deprecated alias).
- `review_max_diff_lines`: runs whose diff exceeds it stop with `diff_too_large`.
- `docs/ISOLATION.md` with reference wrapper configurations.
- `doctor` warnings for missing prices and for `env`-only check isolation.
- More secret patterns in the feedback sanitizer (npm, Stripe, Azure, Slack webhook, Google
  service-account JSON).

### Changed
- One name everywhere: package, module and command are `reoengine`; environment variables are
  `REOENGINE_*`.
- The research result is now passed to the architect and the coder.
- Check processes run with a `PATH` stripped of relative entries and of entries inside the worktree.

### Fixed
- `--research` no longer discards its result.
- Removed a nonexistent `fix` role from the fix-loop budget accounting.

## [0.1.0] - 2026-11-07

First public release.

- Checkpointed pipeline: preflight, brief, research, plan, implement, gates, review, fix loop, accept.
- Deterministic gates: path rules, project checks, red/green TDD check, outcome classification.
- Independent review by digest, with full-patch review for high-risk or large changes.
- Human approval: the engine never commits, pushes or merges.
- Vendor CLI adapters (`codex`, `claude`, `agy`) and HTTP adapters (`gemini`, `anthropic`, `openai`)
  for JSON-answering roles. Only the Gemini request format is checked against provider documentation.
- Outside-write guard, secret-name checks, feedback sanitizer, optional wrapper isolation.
- Per-call usage and cost accounting, fix-loop token budget and no-progress stop.
