# AGENTS.md

Instructions for AI coding agents working in this repository. Humans: see
[CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

A local-first MCP server (Python) that turns narrated screen recordings into
agent-ready data: local Whisper transcript, scene keyframes, OCR text,
wall-clock anchoring. No LLM inside the server. Recordings never leave the
machine — keep it that way.

## Commands

```bash
uv sync                        # env (python pinned via .python-version)
uv run pytest tests/unit -q    # fast suite — run on every change
uv run pytest -q               # full suite (downloads whisper tiny once)
uv run ruff check              # lint (must be clean)
uv run mypy src                # strict typing (must be clean)
uv run python scripts/gen_integrations.py   # regenerate ALL engine artifacts
```

CI = ruff + mypy + unit + integration + e2e on ubuntu; ruff + unit on macos.

## Architecture in one breath

`src/talkthrough_mcp/core/` — deterministic pipeline (no MCP imports):
probe → wallclock → stt → frames → dedup → ocr → manifest → jobs, orchestrated
by `core/pipeline.py`. `server.py` — thin FastMCP layer (7 tools, 5 prompts).
`cli.py` — serve/process/gc. Full map: `docs/DESIGN.md`.

## Hard rules

1. **`guidance.py` is the single source of truth** for tool descriptions and
   prompt templates. Never edit a tool description, prompt, example prompt
   file, or plugin command anywhere else.
2. **Generated files are never hand-edited.** `examples/prompts/`,
   `integrations/**` (except `claude-desktop/manifest.json`), and `.mcp.json`
   are rendered by `scripts/gen_integrations.py`; edit the generator (or the
   canonical sources below) and re-run it. A drift test byte-pins every
   generated file.
3. Canonical sources mirrored by the generator: the Agent Skill at
   `.agents/skills/talkthrough/SKILL.md` and the example agent at
   `examples/agents/feedback-triage.md`.
4. New/changed tools ship guidance: 10–15 one-line examples (≤120 chars) in
   the description — gated by `tests/unit/test_guidance.py`.
5. Tool responses stay token-budgeted: paginate or cap any new output
   (see "Token-budget rules" in `docs/DESIGN.md`).
6. Privacy: no runtime network beyond one-time model/tool downloads, no
   telemetry.
7. Tests accompany changes: unit for pure logic; integration when behavior
   needs real ffmpeg/whisper (fixtures are committed; regenerating them is
   macOS-only — `tests/fixtures/make_fixtures.py` — and must update
   `tests/integration/fixture_facts.py` together).
8. Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Layout signals

- `integrations/<engine>/` — per-engine install adapters (generated).
- `integrations/claude-code/` — the Claude Code plugin body; the marketplace
  manifest at `.claude-plugin/marketplace.json` points here.
- `.agents/skills/talkthrough/` — cross-engine Agent Skill (canonical).
- `.mcp.json` at repo root runs the LOCAL dev server for agents working in
  this checkout (`talkthrough-dev`).
