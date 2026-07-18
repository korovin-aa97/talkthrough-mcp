---
name: talkthrough
description: Analyze narrated screen recordings and audio files through the talkthrough MCP server — triage feedback into findings, extract specs/backlogs/action items from recordings, and correlate spoken remarks with logs via wall-clock timestamps. Use when the user mentions a screen recording, screencast, narrated video/audio file, or asks to "watch" a recording and act on it.
license: MIT
metadata:
  author: korovin-aa97
  repository: https://github.com/korovin-aa97/talkthrough-mcp
---

# Analyzing narrated recordings with talkthrough

The talkthrough MCP server turns a local video/audio file into queryable
structured data: timestamped transcript segments, scene keyframes, OCR'd
on-screen text, and wall-clock anchoring. No LLM inside — you bring the
reasoning; it brings the evidence. Everything is lazy and token-budgeted:
never ask for more than the moment you are analyzing.

## Prerequisite

The `talkthrough` MCP server must be connected (tools like
`process_media` / `get_transcript` are visible). If not, tell the user to
install it: `claude mcp add -s user talkthrough -- uvx talkthrough-mcp`
(see the repository README for other clients).

## Core workflow

1. **Ingest once**: `process_media(path)` — idempotent by content hash;
   re-calls on the same file return instantly. Long videos take minutes and
   stream progress. The summary gives you `job_id`, counts, wall-clock, and
   a transcript preview — do NOT dump anything else eagerly. Multi-person
   recording (meeting/interview)? Add `diarize=true` — even when the ask is
   just "summarize", speaker structure is part of meeting analysis — and — whenever the
   headcount is known — `num_speakers=N` (the main accuracy lever): segments
   get `S1`/`S2`/… labels and the summary a talk-time roster. On an
   already-processed job this amends in seconds without re-transcribing.
2. **Orient**: `get_transcript(job_id)` (paginate via `next_start_ms` when
   `truncated`) or `search(job_id, "<distinctive word>")` to jump straight
   to the relevant moments (searches speech AND on-screen OCR text).
3. **Evidence per remark**: `get_moment(job_id, t0-2000, t1+2000)` — one
   call returns the transcript slice + up to 3 unique frames + their OCR
   text + the wall-clock range. This is the workhorse; describe `observed`
   from the returned pixels, never from imagination.
4. **Precision when needed**: `get_frames(at_ms=...)` for nearby keyframes;
   `extract_frame(job_id, at_ms, crop={x,y,w,h})` for an exact instant at
   native resolution (keyframes capture scene changes + a 1 fps floor, so
   sub-second moments can fall between them).
5. **Recall across sessions**: `list_jobs()` — the store persists; a file
   processed yesterday (even via CLI) is queryable by `job_id` today.

## Timestamps

Every timestamped result carries `t_ms` (video-relative) and, when the
recording start is known, `t_wall` (ISO 8601 real time). Copy `t_wall`
VERBATIM from the payload — never compute it from `t_ms` yourself
(hand-derived wall-clocks drift by whole hours). Use `t_wall` to
correlate remarks with server/app logs (±30 s grep window). If
`wall_clock` is null or low-confidence, ask the user when the recording
started and re-anchor: `process_media(path, recorded_at="<ISO 8601>",
force=true)`.

## Packaged workflows (server prompts)

Prefer the server prompts when the task matches — they encode the full
method: `triage-recording` (screencast → findings JSON per the contract in
`examples/output-contract.schema.json`), `spec-from-workshop`,
`backlog-from-demo`, `meeting-actions` (audio-only friendly),
`correlate-with-logs`.

## Rules of thumb

- Audio-only jobs (.m4a/.mp3/…): transcript tools work; frame tools error
  by design — that error is expected, not a failure.
- Speaker labels are anonymous (`S1`/`S2`, ordered by first voice). Mapping
  them to names is YOUR job: self-introductions, vocatives, the attendees
  list — and on video jobs the screen check is MANDATORY: for every label
  you map, `get_frames(at_ms=<that label's longest_turn_ms from the
  roster>)` and read the meeting-app name plates, the recording's title
  card, the active-speaker highlight BEFORE asserting the mapping. STT
  homophones lie about name spellings (spoken "profit" vs on-screen
  "Prophet") — trust OCR/frames over the transcript for names. State the
  mapping explicitly and mark unmapped labels "unidentified".
  `diarize=true` needs the `[diarization]` extra — its absence produces an
  actionable install-hint error.
- Findings/quotes must cite the narrator's exact words + `t_ms` (+ `t_wall`
  when known) + the frame files you actually inspected.
- Low STT/vision confidence → surface a question; never silently guess.
- Any narration language works (Whisper auto-detects; the summary reports
  `language` + `language_probability`). Garbled transcript or low/wrong
  detection → re-call `process_media(path, model="large-v3-turbo",
  force=true)` (best multilingual quality) or pin `language="…"`; domain
  jargon → pass `vocabulary="Term1, Term2"`.
- Write digests/summaries for the recording author in the narrator's
  language; keep quotes verbatim in the original — translate in your own
  prose only, never inside a quote.
