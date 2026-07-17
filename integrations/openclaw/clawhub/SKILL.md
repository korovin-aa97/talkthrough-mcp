---
name: talkthrough
description: Analyze narrated screen recordings and audio files — timestamped transcript, scene keyframes, OCR text, and wall-clock anchoring via the local talkthrough MCP server. Use when the user shares a recording or asks to triage feedback, extract meeting actions, or correlate a recording with logs.
---

# talkthrough for OpenClaw

This skill wires the talkthrough MCP server into OpenClaw and teaches the
workflow. Everything runs locally: recordings never leave the machine.

## Setup (once)

Add the MCP server:

```bash
openclaw mcp add talkthrough --command uvx --arg talkthrough-mcp[diarization]
```

Requires `uv` (https://astral.sh/uv). First processing downloads a whisper
model once (~460 MB for the default `small`).

## Workflow

1. `process_media(path)` — idempotent by content hash; returns a compact
   summary with job_id (re-calls on the same file are instant).
2. `get_transcript(job_id)` / `search(job_id, "<word>")` — orient; search
   covers speech AND on-screen OCR text.
3. `get_moment(job_id, t0-2000, t1+2000)` — evidence bundle per remark:
   transcript slice + up to 3 frames + OCR + wall-clock range.
4. `extract_frame(job_id, at_ms, crop=...)` — exact instant, native
   resolution, when keyframes miss the moment.
5. `list_jobs()` — recordings processed earlier remain queryable.

Timestamps: `t_ms` is video-relative; `t_wall` is real wall-clock time when
the recording start is known — use it to correlate remarks with logs
(±30 s window). Audio-only files (.m4a/.mp3/…) have transcripts but no
frames; frame tools erroring on them is expected.

Full docs: https://github.com/korovin-aa97/talkthrough-mcp
