# Jarvis-hermes: instructions for coding agents

Read CONTEXT.md, docs/specs/jarvis-v1.md and the relevant ADR before implementation.
This is one assistant, MCP-first, using Hermes and the existing Telegram bot.
Do not invent SDK capabilities, model availability or successful host integration.
Do not start a second poller or replace an existing Hermes installation without inspecting it.
Never commit credentials, personal vault contents, runtime databases or screenshots.
Every future execution path (MCP, terminal, browser, GUI) must respect the same policy.

## Verification

- `uv sync --locked`
- `uv run python -m unittest discover -s tests -v`
- `uv run jarvis doctor --json`

The doctor reports its current execution host, not the user's Windows computer.
Run host-dependent acceptance tests on the real Windows/WSL host before closing their issues.

## Agent skills

### Issue tracker

GitHub Issues in nuno80/Jarvis-hermes. See docs/agents/issue-tracker.md.

### Domain docs

Single-context: CONTEXT.md and docs/adr/. See docs/agents/domain.md.

Use Matt Pocock skills installed in the coding agent. Skill source is not vendored here.
The 30 tickets were prepared with to-tickets and publication authorized by the owner.
