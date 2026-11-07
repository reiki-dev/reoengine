# Check isolation wrappers

Wrapper mode runs project checks behind a command prefix from `limits.toml`. It lets you add a network and filesystem boundary around checks; the engine does not provide that boundary itself. The wrapper receives the check command after its configured arguments, and `{worktree}` is replaced with the active worktree path.

Example Linux `bwrap` configuration:

```toml
[isolation]
mode = "wrapper"
wrapper = ["bwrap", "--ro-bind", "/", "/", "--bind", "{worktree}", "{worktree}", "--unshare-net", "--chdir", "{worktree}", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
```

Example Docker configuration (replace `<image>` with a local image):

```toml
[isolation]
mode = "wrapper"
wrapper = ["docker", "run", "--rm", "--network", "none", "-v", "{worktree}:{worktree}", "-w", "{worktree}", "<image>"]
```

The Docker image must contain the project's toolchain. Check both examples in your own environment before relying on them; host paths, permissions, devices, and toolchains vary.

The outside-write guard monitors selected run-state files and run links, the configured config
directory, three named files in the engine state directory, and Git status plus persistent Git
metadata. It hashes ordinary config/state files and selected small Git control files. Ignored files,
Git refs, secret-named files, links, and other Git metadata are checked by type, size, and
modification time only; those values can be forged. Source file contents are intentionally never
opened. `events.jsonl` must have identical size and SHA-256 before and after an agent call.

POSIX process cleanup is best effort without cgroups. Hostile descendants can escape using
`setsid`, a scrubbed process-token environment, or a double fork.

The TDD red/green gate only provides evidence of failed and passed checks with assertion-like
output, not proof of meaningful regression coverage. Secret redaction is best-effort pattern
matching, and infrastructure-versus-assertion classification is heuristic.
