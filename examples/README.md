# Examples: composing talkthrough with your stack

The server extracts structure; your agent decides what to do with it. These
files are starting points.

## What's here

- [`agents/feedback-triage.md`](agents/feedback-triage.md) — a ready-to-use
  Claude Code agent: recording → findings JSON → numbered confirm digest.
  Drop it into `.claude/agents/` in your project.
- [`prompts/`](prompts/) — the five workflow prompts, byte-identical to the
  server-side MCP prompts (`triage-recording`, `spec-from-workshop`,
  `backlog-from-demo`, `meeting-actions`, `correlate-with-logs`). Use them
  directly from any MCP client as slash commands, or paste them into clients
  that don't surface MCP prompts.
- [`output-contract.schema.json`](output-contract.schema.json) — the findings
  contract the triage flow emits. Validate agent output against it before
  filing anything.

## Composition patterns

**File bugs to GitHub Issues.** Run `triage-recording`, have the user approve
digest items, then create one issue per approved finding with the `gh` CLI:
title from `title`, body from `quote`/`observed`/`expected`/
`acceptance_criteria`/`verify_via`, and attach the referenced frames from
`~/.talkthrough/jobs/<job_id>/frames/`.

**File to Jira via the Atlassian MCP.** Same flow; map `severity` P1-P3 to
your priority scheme and put `t_wall` in the description so teammates can
correlate with logs and session replays.

**Build a backlog document.** `backlog-from-demo` produces a prioritized
markdown table with timestamped evidence — commit it to your repo or paste it
into your planning tool.

**Meeting follow-ups.** `meeting-actions` works on audio-only files (`.m4a`,
`.mp3`, …): action items with owners and quotes, ready for your task tracker.

**Debug sessions.** `correlate-with-logs` turns "it hung right here" into a
±30 s wall-clock grep window over your server logs.

## Tips

- Long recordings: pre-process once with the CLI
  (`talkthrough-mcp process session.mov`), then agents query the job instantly
  by id — the store is content-addressed.
- Domain jargon: pass `vocabulary="YourProduct, PgBouncer, OKR"` to
  `process_media` so product names survive transcription.
- Non-English narration: set `TALKTHROUGH_WHISPER_MODEL=medium` (or
  `large-v3`) in the server's environment.
