# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's **private vulnerability reporting** on this repository (Security tab, "Report a
vulnerability"). If it is not enabled, open an issue that asks for a private channel and contains
no details. Include the version
(`reona.__version__` or the installed package version), the platform, and a minimal reproduction using
synthetic data only. Never send real secrets, API keys or customer data.

You can expect an acknowledgement within a few working days. This is an early-stage project
maintained by a small team, and no formal SLA is offered.

## Scope

In scope: the engine in `engine/reona`, its default configuration in `config/`, and the
documentation claims in `docs/SECURITY_AND_COMPLIANCE.md`.

Out of scope: vulnerabilities in the third-party CLIs and APIs the engine drives (Codex CLI,
Claude Code, Antigravity `agy`, provider HTTP APIs), and issues that require an already
compromised host. Read the limits in the security model before assuming a guarantee.
