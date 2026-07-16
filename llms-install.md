# Installing talkthrough-mcp (instructions for AI agents)

You are installing `talkthrough-mcp`, a local-first MCP stdio server that
turns narrated screen recordings / audio files into queryable structured data
(timestamped transcript, scene keyframes, OCR text, wall-clock anchoring).

## Requirements

- macOS or Linux. Python is NOT required system-wide — `uv`/`uvx` manages it.
- `uv` must be installed. If missing:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- No other system dependencies: ffmpeg auto-falls back to a bundled build,
  OCR is pip-only, whisper models download on first use (~460 MB for the
  default `small`, one-time).

## Server command (stdio)

```
uvx talkthrough-mcp
```

(To run the latest unreleased main instead:
`uvx --from git+https://github.com/korovin-aa97/talkthrough-mcp talkthrough-mcp`)

## Client configuration

### Claude Code

```bash
claude mcp add -s user talkthrough -- uvx talkthrough-mcp
```

### Claude Desktop / Cursor / Cline / any JSON-config client

Add to the client's MCP servers config (`claude_desktop_config.json`,
`~/.cursor/mcp.json`, `cline_mcp_settings.json`, …):

```json
{
  "mcpServers": {
    "talkthrough": {
      "command": "uvx",
      "args": ["talkthrough-mcp"]
    }
  }
}
```

Optional env vars (add an `"env"` object next to `"args"` if the user needs
them): `TALKTHROUGH_WHISPER_MODEL` (default `small`; use `large-v3-turbo`
for non-English narration — the `process_media` tool also accepts a per-call
`model` parameter), `TALKTHROUGH_OCR` (`off` to disable),
`TALKTHROUGH_HOME` (job store root, default `~/.talkthrough`).

## Optional: speaker diarization (who said what)

If the user wants speaker labels on meetings/interviews, install with the
extra — replace the package name in ANY config above with
`talkthrough-mcp[diarization]`:

```json
"args": ["talkthrough-mcp[diarization]"]
```

(Shell commands need quotes: `uvx "talkthrough-mcp[diarization]"`.) Then
`process_media(path=..., diarize=true, num_speakers=<count>)` labels segments
`S1`/`S2`/…. Pass `num_speakers` whenever the participant count is known —
it is the main accuracy lever. Calling `diarize=true` on an
already-processed job adds speakers without re-transcribing. Diarization
models (~47 MB) download once, pinned and checksum-verified.

## Verify the installation

1. The client should list 7 tools: `process_media`, `get_transcript`,
   `get_frames`, `get_moment`, `search`, `extract_frame`, `list_jobs`, and
   5 prompts (`triage-recording`, `spec-from-workshop`, `backlog-from-demo`,
   `meeting-actions`, `correlate-with-logs`).
2. Smoke test: call `list_jobs()` — an empty result is a healthy first run.
3. Full test (optional): `process_media(path="<any short local .mp4/.mov/.m4a>")`
   → expect a summary with `job_id`. First run downloads the whisper model;
   allow several minutes.

## Troubleshooting

- Connection fails → run the server command from "Server command" directly in
  a terminal; read stderr (protocol goes to stdout, logs to stderr).
- First `process_media` is slow → one-time model/ffmpeg downloads; subsequent
  runs are fast and re-processing the same file is instant (content-addressed
  store).
- Frame tools error on `.m4a`/`.mp3` inputs → expected: audio-only jobs have
  transcripts but no frames.

More context for usage (not installation): `.agents/skills/talkthrough/SKILL.md`
and the tool reference in `README.md`. Per-engine config examples:
`integrations/<engine>/README.md`.
