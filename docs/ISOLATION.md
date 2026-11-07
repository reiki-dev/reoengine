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
