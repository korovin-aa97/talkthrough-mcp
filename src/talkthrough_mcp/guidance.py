"""Single source of truth for the tool guidance layer.

Two mechanisms, both mandatory:

1. Per-tool usage examples embedded in every tool description
   (``TOOL_DESCRIPTIONS``): purpose, a "when NOT to use" line, then 10-15
   one-line examples (canonical calls, param combos, agent intents mapped to
   the right call, edge cases, anti-examples redirecting to the better
   tool). One-liners only — the full tool list lands in every client's
   context window.

2. Server-side MCP prompts (``PROMPT_TEMPLATES``): five workflow prompts
   invocable as slash commands from MCP clients. The files in
   ``examples/prompts/`` render from these same templates — a unit test
   pins them together so they cannot drift.

Everything here is a constant so ``tests/unit/test_guidance.py`` can gate
example counts, line lengths, and prompt/file equivalence.
"""

from __future__ import annotations

EXAMPLES_HEADER = "Examples:"

TOOL_DESCRIPTIONS: dict[str, str] = {
    "process_media": """\
Ingest a LOCAL video or audio file and make it queryable: validates the file, transcribes \
speech locally (whisper), extracts scene-change keyframes, OCRs on-screen text, and resolves \
the wall-clock start time. Returns a compact summary (job_id, media info, wall_clock, \
transcript preview) — full data stays on disk and is served lazily by the other tools. \
Idempotent by content hash: re-calling on an already-processed file returns instantly.
When NOT to use: to re-fetch data you already processed (use the retrieval tools), or for \
URLs — local file paths only.
Examples:
- process_media(path="/Users/sam/Desktop/bug-repro.mov") — narrated screencast, defaults are right
- process_media(path="~/Videos/demo.mp4", language="en") — pin the language, skip auto-detect
- process_media(path="/rec/interview.mov", model="large-v3-turbo") — best multilingual quality (1.5 GB, one-time)
- process_media(path="/tmp/standup.m4a") — audio-only: transcript tools work, frame tools will error
- process_media(path="/rec/review.mov", vocabulary="OKR, PgBouncer, Kanban") — jargon survives STT
- process_media(path="/rec/demo.mov", recorded_at="2026-07-10T12:03:00+02:00") — exact wall-clock anchor
- process_media(path="/rec/demo.mov", recorded_at="2026-07-10T12:03:00+02:00", force=true) — re-anchor a done job
- user: "I just recorded my screen, it's on my Desktop" → process_media(path="/Users/<user>/Desktop/<file>.mov")
- user drops a browser tab capture → process_media(path="~/Downloads/tab-capture.webm")
- summary shows wall_clock=null → ask when recording started, re-call with recorded_at=... and force=true
- transcript garbled or language_probability low → re-call with model="large-v3-turbo" (or language="ru") + force=true
- 30-min video is fine: progress notifications stream while whisper runs; expect minutes, not seconds
- after success, do NOT dump everything — continue with get_transcript / get_moment / search on the job_id
- anti-example: frames from an already-processed job → get_frames(job_id=...), never process_media again
- anti-example: YouTube/URL input → unsupported in v1; have the user download the file first
""",
    "get_transcript": """\
Retrieve the transcript of a processed job, lazily and paginated. Formats: "segments" \
(default — seq, t_ms, t_wall when known, text), "text" (plain prose), "srt" (subtitles). \
Responses are capped (~8k tokens): when truncated=true, continue from the returned \
next_start_ms.
When NOT to use: to find one keyword (use search) or to inspect one moment with visuals \
(use get_moment).
Examples:
- get_transcript(job_id="a1b2c3d4e5f60718") — whole transcript of a short recording
- get_transcript(job_id="a1b2c3d4e5f60718", start_ms=0, end_ms=120000) — just the first two minutes
- get_transcript(job_id="...", format="text") — prose block for summarization
- get_transcript(job_id="...", format="srt") — subtitle export the user asked for
- got truncated=true with next_start_ms=421500 → get_transcript(job_id="...", start_ms=421500)
- user: "what was said between 5:00 and 6:30?" → start_ms=300000, end_ms=390000
- meeting recording (audio-only job): this tool is the main surface — frames don't exist there
- correlate speech with logs: each segment's t_wall lines up with your log timestamps
- wall_clock=null on the job → segments carry t_ms only (relative to video start)
- 60-min video: page by ranges (e.g. 10-min windows), don't pull from 0 repeatedly
- anti-example: "where did they mention checkout?" → search(job_id, "checkout"), not full paging
- anti-example: screenshots around a remark → get_moment(job_id, start_ms, end_ms)
""",
    "get_frames": """\
Fetch stored keyframe images (JPEG, <=1568px wide) as MCP image content: the frames nearest \
to at_ms, OR unique frames across [start_ms, end_ms] evenly thinned to max_frames. Serves \
unique frames by default (near-duplicates from static scenes are filtered); hard cap 6 \
images per call.
When NOT to use: exact instants between keyframes or native-resolution detail (use \
extract_frame), or finding on-screen text (use search — OCR text is indexed).
Examples:
- get_frames(job_id="...", at_ms=83500) — what was on screen when the remark at 1:23.5 was spoken
- get_frames(job_id="...", at_ms=83500, max_frames=2) — tighter context, fewer tokens
- get_frames(job_id="...", start_ms=0, end_ms=600000, max_frames=6) — overview strip of the first 10 min
- get_frames(job_id="...", start_ms=290000, end_ms=310000, include_duplicates=true) — every capture near 5:00
- transcript hit at t_ms=421500 → get_frames(job_id, at_ms=421500) for the visual evidence
- walking a demo scene by scene → one ranged call per scene beats one giant range
- frame files are named by video-ms (t00083500.jpg ↔ t_ms 83500) — stable refs for findings
- keep max_frames at 2-4 unless you are truly comparing scenes; images are token-expensive
- audio-only job → this tool errors by design; use get_transcript / get_moment instead
- anti-example: need EXACTLY 12:34.500 between two keyframes → extract_frame(job_id, at_ms=754500)
- anti-example: "find the screen with the red error banner" → search(job_id, "error") first, then jump
""",
    "get_moment": """\
The "one remark" evidence bundle: transcript slice + up to 3 unique frames + their OCR text \
+ the wall-clock range for [start_ms, end_ms], in a single call. This is the workhorse for \
triage: one call per finding gives you the quote, the screenshot, and the on-screen text.
When NOT to use: broad exploration (get_transcript / get_frames) or keyword lookup (search).
Examples:
- get_moment(job_id="...", start_ms=83000, end_ms=97000) — full evidence for the remark at 1:23-1:37
- segment seq 12 spans t0_ms=83210, t1_ms=96800 → get_moment(job_id, 83210, 96800)
- pad ±2000 ms around the spoken range — narrators react to things already on screen
- triage loop: for each candidate finding, exactly one get_moment call → quote + frame + OCR
- user: "what was I showing when I said 'this button is broken'?" → search first, then get_moment at the hit
- opening context of a meeting: get_moment(job_id, 0, 15000)
- response includes the t_wall range when known → quote it in bug reports for log correlation
- audio-only job → returns the transcript slice plus a no-frames note (that is expected)
- anti-example: whole-video summary → get_transcript(format="text"), not a chain of get_moments
- anti-example: need more than 3 frames of a range → get_frames(start_ms=..., end_ms=..., max_frames=6)
- keep ranges under ~30 s; a 5-min "moment" dilutes the bundle and wastes tokens
""",
    "search": """\
Case-insensitive substring search across BOTH transcript segments and frame OCR text. Hits \
carry source (transcript|ocr), t_ms, t_wall when known, the matched text, and the nearest \
frame position — everything needed to jump straight to evidence. Exact substring only, no \
embeddings.
When NOT to use: fuzzy/semantic questions ("anything about performance?") — page \
get_transcript and read; regex is not supported.
Examples:
- search(job_id="...", query="login") — every spoken or on-screen mention of login
- user: "what did I say about the login button?" → search(job_id, "login button") → get_moment at hits
- search(job_id, "error") — catches the SPOKEN word and the on-screen error text (OCR) in one call
- search(job_id, "TypeError") — stack traces on screen are OCR-indexed; great for bug repros
- search(job_id, "€49") — prices, IDs, and literals on screen are findable via OCR
- take hit.t_wall and grep your server logs ±30 s around it to pair remark ↔ log line
- no hits? shorten the stem: "notif" matches notification / notifications / notify
- prefer one distinctive word ("checkout") over a whole sentence — substrings must match exactly
- every hit has nearest_frame_ms → get_frames(job_id, at_ms=<that>) shows the moment
- audio-only job → transcript hits only (there is no OCR index)
- anti-example: "summarize the pricing discussion" → get_transcript(format="text") and read it
- anti-example: finding an icon or layout glitch with no text → get_frames over the range; OCR sees text only
""",
    "extract_frame": """\
Re-extract ONE frame at an exact timestamp from the ORIGINAL source video at native \
resolution, with an optional crop {x, y, w, h} in source pixels. Use when the stored \
keyframes miss the instant (they capture scene changes + a 1 fps floor) or when you need \
full-resolution detail. Slower than get_frames — it decodes the source file, which must \
still exist at its recorded path.
When NOT to use: normal browsing — get_frames serves stored keyframes instantly without \
touching the source.
Examples:
- keyframes sit at 12:31 and 12:38 but the flash happened at 12:34.5 → extract_frame(job_id, at_ms=754500)
- extract_frame(job_id="...", at_ms=754500, crop={"x":800,"y":40,"w":400,"h":120}) — zoom into the toast text
- tiny UI text unreadable in the 1568px keyframe → extract_frame at the same ms for native resolution
- verify a one-frame glitch: extract_frame at 12300, 12400, 12500 and compare
- OCR missed small text → extract_frame with a tight crop, then read the returned image
- crop coordinates are SOURCE pixels (a Retina screen recording may be 2940x1912) — not keyframe scale
- source file moved or deleted → clear error; stored keyframes via get_frames still work
- audio-only job → always errors: there is no video stream to decode
- anti-example: "show me around 5:00" → get_frames(job_id, at_ms=300000); extract_frame is for exact instants
- anti-example: scanning a range frame by frame → get_frames(start_ms, end_ms) first, refine once after
""",
    "list_jobs": """\
List processed recordings, newest first: job_id, source filename, duration, created, \
wall-clock start, segment/frame counts. The store is content-addressed — the same file maps \
to the same job even after renames or moves, and jobs persist across sessions and machines \
restarts.
When NOT to use: as a health check or before every call — job_ids are stable, remember them.
Examples:
- user: "triage the recording I processed this morning" → list_jobs() → pick by filename + created
- user names neither job_id nor path → list_jobs() first; only ask if still ambiguous
- resume yesterday's analysis in a fresh conversation → list_jobs() → reuse its job_id directly
- file was renamed after processing → match by duration/created; the content hash ignores names
- wall_clock.start answers "WHEN was this session?" — pick the job from "yesterday around 15:00"
- after CLI batch pre-processing (`talkthrough-mcp process big.mov`) the job shows up here — query it
- two jobs with the same filename → the newer created one is usually the re-recording
- empty list → nothing processed on this machine yet; ask the user for a file path
- job disappeared → likely `talkthrough-mcp gc` cleaned it; re-run process_media on the file (same id)
- anti-example: checking whether a NEW file is processed → just call process_media, it is idempotent+instant
""",
}

TOOL_NAMES = tuple(TOOL_DESCRIPTIONS)

# --- server prompts ----------------------------------------------------------

PROMPT_NAMES = (
    "triage-recording",
    "spec-from-workshop",
    "backlog-from-demo",
    "meeting-actions",
    "correlate-with-logs",
)

PROMPT_DESCRIPTIONS: dict[str, str] = {
    "triage-recording": (
        "Turn a narrated screencast into precise, evidence-backed findings JSON "
        "(bug / feature / question routing with frame evidence)."
    ),
    "spec-from-workshop": (
        "Turn a recorded workshop or design walkthrough into a structured spec "
        "with quoted decisions and open questions."
    ),
    "backlog-from-demo": (
        "Turn a recorded product demo into a prioritized backlog with timestamped evidence."
    ),
    "meeting-actions": (
        "Turn a recorded meeting (audio is enough) into action items, decisions, "
        "and open questions with timestamps."
    ),
    "correlate-with-logs": (
        "Walk a recording's remarks against system logs using wall-clock timestamps."
    ),
}

_PROMPT_CONTEXT_LABELS: dict[str, str] = {
    "triage-recording": "Product context",
    "spec-from-workshop": "Feature name",
    "backlog-from-demo": "Project context",
    "meeting-actions": "Attendees",
    "correlate-with-logs": "Log source",
}

PROMPT_TEMPLATES: dict[str, str] = {
    "triage-recording": """\
You are a meticulous triage agent. A narrated screen recording was processed by
talkthrough as job `{job_id}`. Turn it into precise, evidence-backed findings.
{context_section}
## Method

1. If you do not have the job summary yet, call get_transcript(job_id="{job_id}")
   (paginate via next_start_ms if truncated). If the job_id looks wrong, verify with
   list_jobs().
2. Walk the transcript in order. Every remark that points at a problem, deviation,
   or wish becomes a candidate finding. The narrator's words are directives, not
   suggestions.
3. For each candidate, call get_moment(job_id="{job_id}", start_ms=<t0-2000>,
   end_ms=<t1+2000>). Describe `observed` from the returned frames and OCR text —
   from the pixels, never from imagination.
4. Cross-check: use search(job_id="{job_id}", query="<distinctive word>") to find
   repeat mentions of the same issue and merge them into one finding.
5. If a frame is ambiguous, call extract_frame(job_id="{job_id}", at_ms=<exact ms>,
   crop=<region>) for a full-resolution look before judging.

## Output contract

Return ONE fenced JSON object, no other prose:

- `findings`: array of objects with: `title`, `quote` (narrator's exact words),
  `t_ms`, `t_wall` (null when unknown), `frame_refs` (frame files you actually
  looked at), `observed`, `expected`, `acceptance_criteria` (verifiable list),
  `verify_via` (the exact flow to reproduce), `route` = "bug" | "feature" |
  "question", `severity` = "P1" (flow broken) | "P2" (default) | "P3" (polish),
  `confidence` = "high" | "medium" | "low", and `question` (only when
  route="question").
- `digest`: a numbered one-line-per-finding summary the recording author can
  answer with "1,3-4".
- `key_frames`: 2-3 frame refs that best show the problems.

Rules: low STT/vision confidence → route="question" with a concrete question —
never a silent guess. Findings without frame evidence (audio-only jobs) must say
so in `frame_refs: []`. Write `digest` in the narrator's language (the
transcript language); keep every `quote` verbatim in the original language —
never translate quotes.
""",
    "spec-from-workshop": """\
You are a product engineer writing a spec from a recorded workshop / design
walkthrough, processed by talkthrough as job `{job_id}`.
{context_section}
## Method

1. Read the whole discussion: get_transcript(job_id="{job_id}", format="text");
   for long sessions page with start_ms/end_ms via the segments format instead.
2. Identify decision points and requirement statements. For each, keep the
   speaker's exact wording and t_ms; re-read the precise slice with
   get_transcript(job_id="{job_id}", start_ms=..., end_ms=...) when in doubt.
3. Whenever the discussion references something visual (whiteboard, mockup,
   screen), pull it: get_frames(job_id="{job_id}", at_ms=<that moment>) — or
   get_moment(job_id="{job_id}", start_ms=..., end_ms=...) for slice + frames +
   OCR in one call.
4. Use search(job_id="{job_id}", query="<term>") to collect every mention of a
   contested term before writing its section.

## Output

A markdown spec with these sections:

1. **Goal** — one paragraph, in the participants' own framing.
2. **Decisions** — bullet list; each bullet: the decision, a supporting quote,
   `t_ms`/`t_wall`.
3. **Requirements** — numbered, testable statements; mark each MUST/SHOULD/COULD.
4. **Visual references** — frame refs for every screen/mockup discussed.
5. **Open questions** — everything ambiguous or contested, with the quote that
   raised it. Do not resolve ambiguity yourself — surface it.

Write the spec in the workshop's language; quotes stay verbatim.
""",
    "backlog-from-demo": """\
You are a product owner turning a recorded product demo into a prioritized
backlog. The demo was processed by talkthrough as job `{job_id}`.
{context_section}
## Method

1. get_transcript(job_id="{job_id}") — first pass; note every feature shown,
   every rough edge, every "we should…" remark.
2. For each candidate backlog item, call get_moment(job_id="{job_id}",
   start_ms=..., end_ms=...) around the remark to capture what the screen
   actually showed (frames + OCR).
3. search(job_id="{job_id}", query="<feature term>") to gather all mentions of
   the same capability before writing its item.
4. Use the wall-clock (t_wall) values in evidence so stakeholders can find the
   demo moment in their calendars/notes.

## Output

A markdown backlog table, ordered by priority, one row per item:

| # | Title | User story | Evidence (quote + t_ms + frame ref) | Effort (S/M/L) | Priority (P1-P3) |

Below the table: a "Cut lines" section — items explicitly deferred in the demo,
each with the deferring quote and timestamp. Every row MUST carry real evidence
from the recording; no invented items. Write items in the demo's language;
evidence quotes stay verbatim.
""",
    "meeting-actions": """\
You are taking minutes from a recorded meeting processed by talkthrough as job
`{job_id}`. Audio-only jobs are expected here — frame tools are unavailable for
them, and that is fine.
{context_section}
## Method

1. get_transcript(job_id="{job_id}", format="segments") — walk the whole meeting
   (paginate via next_start_ms when truncated).
2. Collect: action items (who committed to what), decisions (what was agreed),
   open questions (raised but unresolved). Keep exact quotes and t_ms for each.
3. search(job_id="{job_id}", query="<name or topic>") to trace scattered
   follow-ups on one topic before summarizing it.
4. If the job has video (a screen-share was recorded), attach visual evidence to
   items that reference the screen via get_moment(job_id="{job_id}",
   start_ms=..., end_ms=...).

## Output

Markdown with three sections:

1. **Action items** — `- [ ] <action> — owner: <name or "unassigned">, due:
   <date or "unspecified">, evidence: "<quote>" (t_ms, t_wall when known)`.
2. **Decisions** — one bullet per decision with the deciding quote + timestamp.
3. **Open questions** — what needs an answer, who raised it, timestamp.

Owners and dates come ONLY from spoken words — never infer them. When unclear,
write "unassigned"/"unspecified". Write the minutes in the meeting's language;
quotes stay verbatim.
""",
    "correlate-with-logs": """\
You are debugging with two evidence streams: a narrated recording (talkthrough
job `{job_id}`) and system logs the user can access. Pair spoken/visible moments
with log lines using wall-clock time.
{context_section}
## Method

1. Confirm the job's wall-clock anchor: list_jobs() shows wall_clock.start and
   its confidence. If wall_clock is null or confidence is "low", ask the user
   for the recording start time, then re-anchor with process_media(path=...,
   recorded_at="<ISO 8601>", force=true) before correlating.
2. Find the incident moments: search(job_id="{job_id}", query="<error term>")
   and/or get_transcript ranges. Every hit carries t_wall.
3. For each moment, compute the log window t_wall ± 30 s and read the user's
   logs there (ask the user to run the grep if you cannot access the log
   source directly).
4. Pull the matching visual state with get_moment(job_id="{job_id}",
   start_ms=..., end_ms=...) — the OCR text often contains the on-screen error
   that names the failing component.

## Output

A markdown incident walkthrough: one section per correlated moment with (a) the
narrator's quote + t_wall, (b) the matching log lines, (c) the frame ref showing
the screen, and (d) your read of cause vs. symptom. Close with a "Confidence and
gaps" note: which correlations are exact (t_wall confidence "exact"/"high") and
which are approximate ("medium"/"low" — mtime-derived anchors drift). Quote
remarks verbatim in their original language.
""",
}


def render_prompt(name: str, job_id: str, extra: str = "") -> str:
    """Render a workflow prompt. ``extra`` is the optional per-prompt context arg."""
    template = PROMPT_TEMPLATES[name]
    if extra.strip():
        label = _PROMPT_CONTEXT_LABELS[name]
        context_section = f"\n{label}: {extra.strip()}\n"
    else:
        context_section = ""
    return template.format(job_id=job_id, context_section=context_section)


def example_lines(description: str) -> list[str]:
    """The one-line examples of a tool description (for the guidance quality gate)."""
    _, _, tail = description.partition(EXAMPLES_HEADER)
    return [line for line in tail.splitlines() if line.startswith("- ")]
