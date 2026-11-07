# Contributing

Thanks for looking. This is an early project; small, focused changes are easiest to review.

## Ground rules

- **Standard library only.** The engine has no runtime dependencies and should stay that way.
- **No real data.** Tests and examples use synthetic data. Never commit keys, tokens, logs from real
  runs, or customer data. Do not paste them into issues either.
- **No live services in the default test run.** Tests use fake CLIs (`engine/tests/fakes/`) and local
  fake HTTP servers. Anything that needs a real provider must be opt-in and skipped by default.
- **Security-relevant changes need tests that fail without the change.** This includes anything in
  `gates.py`, `guard.py`, `procs.py`, `adapters/` and the sanitizer.

## Setup

```bash
python -m pip install ./engine
cd engine
python -m unittest discover -s tests
python -m mypy --strict reoengine        # install mypy first: python -m pip install mypy
```

CI runs the same on Linux and Windows with Python 3.11 and 3.12. Please check both type-check
platforms locally if you touch process or Windows-specific code:
`python -m mypy --strict --platform linux reoengine` and `--platform win32`.

## Pull requests

- One topic per pull request, with tests.
- Explain *why* in the description; the diff shows *what*.
- Update `CHANGELOG.md` and the relevant page in `docs/` when behavior or configuration changes.
- Behavior that the security model describes must still hold; if you change a guarantee or a limit,
  update `docs/SECURITY_AND_COMPLIANCE.md` in the same pull request.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
