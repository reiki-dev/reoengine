# Provider terms and how ReOEngine is meant to be used

This page is **not legal advice**. Provider terms change. Read the current terms yourself before
deploying, and check the items marked "not verified".

ReOEngine does not reimplement any provider's login, does not read or store provider OAuth tokens,
and does not modify the vendor CLIs. It starts the unmodified CLIs you installed
(`codex`, `claude`, `agy`) as child processes, or calls provider HTTP APIs with a key that
**you** supply in an environment variable (BYOK).

## Two modes

| Mode | Authenticates with | Intended for |
|---|---|---|
| **Subscription CLI mode** | The official CLI signed in by the person running ReOEngine | One individual on their own machine, ordinary personal use |
| **API mode (BYOK)** | Your own API key in an environment variable | Teams, organizations, CI, anything serving more than one person |

**Recommendation:** use API mode for anything beyond personal use. Do not share one person's
subscription login across a team, do not run ReOEngine as a service for third parties on a
personal subscription, and do not collect, copy or forward subscription credentials.

## What the providers say (checked 2026-11-07)

**Anthropic (verified from the primary page "Legal and compliance" on code.claude.com).**
- OAuth sign-in is "intended exclusively" for subscription plan purchasers and "ordinary use of
  Claude Code and other native Anthropic applications".
- Developers building products or services should use API key authentication. Anthropic does not
  permit routing requests through Free, Pro, Max or Ultra plan credentials on behalf of users, or
  collecting, storing or intermediating Claude.ai credentials or session tokens.
- An end user may sign in to the unmodified Claude Code binary with their own subscription.
- Advertised plan limits "assume ordinary, individual usage" of Claude Code and the Agent SDK.

**OpenAI (Codex auth page checked; the Terms of Use page could not be fetched: not verified).**
- Codex CLI supports "Sign in with ChatGPT" and "Sign in with an API key".
- OpenAI documents API key authentication as the recommended default for programmatic and CI use.
- Read the current OpenAI Terms of Use yourself, in particular on automation and on sharing access.

**Google (Gemini API Additional Terms, effective 2026-03-23, checked).**
- The API is for developers building for professional or business purposes, not consumer use.
- You may not use the Services to develop models that compete with them.
- You may not "automatically bypass any requests for human confirmation". ReOEngine never
  commits, pushes or merges on its own: every run ends in `awaiting_human` and needs
  `reoengine approve`.
- Grounded Results have their own restrictions; ReOEngine does not use grounding.
- The Antigravity `agy` CLI and consumer Gemini plans have separate terms: not verified.

## Not verified

- OpenAI Terms of Use (primary text).
- Terms of the Antigravity `agy` CLI and of consumer Google plans.
- Whether a particular automated pipeline counts as "ordinary, individual usage" for a given plan.
  If this matters to you, ask the provider or use API keys.

## Trademarks

Codex, ChatGPT, OpenAI, Claude, Claude Code, Anthropic, Gemini, Google and Antigravity are
trademarks of their respective holders. ReOEngine is an independent project and is not affiliated with or
endorsed by any of them.
