# OpenClaw

Server command (stdio): `uvx "talkthrough-mcp[diarization]"`

Config: `~/.openclaw/openclaw.json`

```json
{
  "mcp": {
    "servers": {
      "talkthrough": {
        "command": "uvx",
        "args": [
          "talkthrough-mcp[diarization]"
        ]
      }
    }
  }
}
```

Or via CLI:

```bash
openclaw mcp add talkthrough --command uvx --arg talkthrough-mcp[diarization]
```

ClawHub: a publish-ready skill wrapper lives in [`clawhub/`](clawhub/) (submit after the repo is public).

Optional env vars: TALKTHROUGH_WHISPER_MODEL (default `small`; use `large-v3-turbo` for non-English narration — agents can also pass `model=` per call), TALKTHROUGH_OCR (`off` to disable), TALKTHROUGH_OCR_LANG (on-screen-text script, e.g. `ru`, `ja`, `ko`), TALKTHROUGH_HOME (job store root, default `~/.talkthrough`). Speaker diarization is included but off per call — agents pass `diarize=true` (plus `num_speakers` when known); the minimal server without the diarization engine is `uvx talkthrough-mcp`.

Verify: the client should list 7 tools (process_media, get_transcript, get_frames, get_moment, search, extract_frame, list_jobs). A `list_jobs` call returning an empty list is a healthy first run.

Engine docs: <https://docs.openclaw.ai/cli/mcp>

Agent-followable install steps for any client: [`llms-install.md`](../../llms-install.md).
