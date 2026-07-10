# Cursor

Server command (stdio): `uvx --from git+https://github.com/korovin-aa97/talkthrough-mcp talkthrough-mcp`

Config: `~/.cursor/mcp.json (or project .cursor/mcp.json)`

```json
{
  "mcpServers": {
    "talkthrough": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/korovin-aa97/talkthrough-mcp",
        "talkthrough-mcp"
      ]
    }
  }
}
```

Optional env vars: TALKTHROUGH_WHISPER_MODEL (default `small`; use `large-v3-turbo` for non-English narration — agents can also pass `model=` per call), TALKTHROUGH_OCR (`off` to disable), TALKTHROUGH_OCR_LANG (on-screen-text script, e.g. `ru`, `ja`, `ko`), TALKTHROUGH_HOME (job store root, default `~/.talkthrough`).

Verify: the client should list 7 tools (process_media, get_transcript, get_frames, get_moment, search, extract_frame, list_jobs). A `list_jobs` call returning an empty list is a healthy first run.

Engine docs: <https://cursor.com/docs/context/mcp>

Agent-followable install steps for any client: [`llms-install.md`](../../llms-install.md).
