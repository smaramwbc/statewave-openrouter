# AGENTS.md — guide for contributors and coding agents

A short orientation for humans and AI coding agents (GitHub Copilot, Claude,
Cursor, …) working in **statewave-openrouter** — an OpenAI-compatible proxy
that injects Statewave memory into OpenRouter calls and writes each turn back.

## Setup, build, test

See the [README](README.md) for canonical setup. In short:

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

Run `ruff` and `pytest` before opening a PR.

## Conventions

- **Code style & testing:** see
  [statewave-docs/dev/conventions.md](https://github.com/smaramwbc/statewave-docs/blob/main/dev/conventions.md).
- **Scope:** this is one file (`statewave_openrouter.py`) that fronts
  OpenRouter. Anything that would be a second service belongs in
  [`smaramwbc/statewave`](https://github.com/smaramwbc/statewave), not here.
  See [REQUIREMENTS.md](REQUIREMENTS.md).
- **This proxy versions independently** of the server and the SDKs; the
  compatibility axis is the `/v1` API contract, not a shared version number.
  Don't align version strings across packages.
- **Memory is opt-in per request.** No subject header means plain
  pass-through; keep it that way.
- **Keep claims accurate and modest** in docs and examples; avoid unqualified
  superlatives.

## Pull requests

Keep PRs focused, add tests for behavior changes, and make sure `ruff` and
`pytest` pass. Issues and feature requests are tracked centrally on
[`smaramwbc/statewave`](https://github.com/smaramwbc/statewave/issues).

## Optional: give your agent memory of this repo (with Statewave)

This project dogfoods Statewave. The easiest way to give your assistant a
queryable project brain for this repo is the **Statewave IDE Companion**
extension for **VS Code / Cursor** (publisher `statewavedev`) — install it from
your editor's extensions marketplace. It exposes your workspace, docs, git
state, and structure to Copilot / Cursor / Claude over MCP and **registers the
MCP server for you** (no manual config); it just needs a Statewave server to
talk to (a one-file `docker compose up`). See the
[extension README](https://github.com/smaramwbc/statewave-connectors/blob/main/packages/vscode-extension/README.md).

Prefer to wire it up by hand, or use another MCP client? Run the
[Statewave MCP server](https://github.com/smaramwbc/statewave-docs/blob/main/connectors/mcp.md)
(`@statewavedev/mcp-server`) directly and query subject
`repo:smaramwbc/statewave-openrouter`.

<!-- statewave:begin (managed by `statewave-connectors mcp init`) -->
**Statewave memory** — MCP server `statewave`, subject `repo:smaramwbc.statewave-openrouter`.
Before answering questions about this project, call `statewave_get_context` (that subject, `query` = the ask) and ground your answer in it.
When the user states a durable fact or decision, call `statewave_ingest_episode` then `statewave_compile_subject` (same subject). Never invent Statewave results.
<!-- statewave:end -->
