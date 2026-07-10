---
description: "Turn a recorded meeting (audio is enough) into action items, decisions, and open questions with timestamps."
---

If no job_id was passed as an argument: call list_jobs() to find the right recording, or process_media(path) if the user gave a file path — then follow the workflow below with that job_id.

You are taking minutes from a recorded meeting processed by talkthrough as job
`$ARGUMENTS`. Audio-only jobs are expected here — frame tools are unavailable for
them, and that is fine.

## Method

1. get_transcript(job_id="$ARGUMENTS", format="segments") — walk the whole meeting
   (paginate via next_start_ms when truncated).
2. Collect: action items (who committed to what), decisions (what was agreed),
   open questions (raised but unresolved). Keep exact quotes and t_ms for each.
3. search(job_id="$ARGUMENTS", query="<name or topic>") to trace scattered
   follow-ups on one topic before summarizing it.
4. If the job has video (a screen-share was recorded), attach visual evidence to
   items that reference the screen via get_moment(job_id="$ARGUMENTS",
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
