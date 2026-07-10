# Contributing

Thanks for taking the time! This project aims to stay small, local-first, and
agent-friendly — contributions that keep it that way are very welcome.

## Dev setup

```bash
git clone https://github.com/korovin-aa97/talkthrough-mcp
cd talkthrough-mcp
uv sync                       # creates .venv with pinned deps (py 3.12)
uv run pytest tests/unit -q   # fast suite, no downloads
```

The full suite (integration + e2e) downloads whisper `tiny` (~75 MB) and a
static ffmpeg build once:

```bash
uv run pytest -q
```

## Checks that must be green

```bash
uv run ruff check
uv run mypy src
uv run pytest -q
```

CI runs the same three on ubuntu (full suite) and macos (lint + unit).

## Layout in one breath

`core/` is the deterministic pipeline (no MCP imports); `server.py` is a thin
FastMCP layer over it; `guidance.py` is the single source of truth for tool
descriptions and prompt templates — **never edit a tool description or prompt
anywhere else**. Everything under `integrations/` (plus `examples/prompts/`
and `.mcp.json`) is rendered by `scripts/gen_integrations.py`; edit the
generator or the canonical sources and re-run it —
`tests/unit/test_guidance.py` byte-pins every generated file, so drift fails
the build. See `docs/DESIGN.md` for the full map and `AGENTS.md` for the
agent-facing version of these rules.

## Guidelines

- Tests come with the change: unit for pure logic, integration when the
  behavior needs real ffmpeg/whisper (fixtures are committed — see
  `tests/fixtures/make_fixtures.py`).
- Keep the privacy promise: no network calls at runtime beyond one-time
  tool/model downloads, no telemetry, recordings never leave the machine.
- Keep responses token-budgeted: new tool output must be paginated or capped
  (see "Token-budget rules" in `docs/DESIGN.md`).
- New tools/prompts must ship guidance: 10-15 one-line examples per tool
  description, and prompts must render from `guidance.py` templates.
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Regenerating fixtures

`tests/fixtures/make_fixtures.py` runs on macOS only (`say`). CI consumes the
committed files; update `tests/integration/fixture_facts.py` together with
any regeneration.

## Not sure where to start?

Issues labeled [`good first issue`](https://github.com/korovin-aa97/talkthrough-mcp/labels/good%20first%20issue)
are scoped to be doable without deep context. Opening an issue to discuss an
idea before coding is always fine.
