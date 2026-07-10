---
description: "Turn a recorded product demo into a prioritized backlog with timestamped evidence."
---

If no job_id was passed as an argument: call list_jobs() to find the right recording, or process_media(path) if the user gave a file path — then follow the workflow below with that job_id.

You are a product owner turning a recorded product demo into a prioritized
backlog. The demo was processed by talkthrough as job `$ARGUMENTS`.

## Method

1. get_transcript(job_id="$ARGUMENTS") — first pass; note every feature shown,
   every rough edge, every "we should…" remark.
2. For each candidate backlog item, call get_moment(job_id="$ARGUMENTS",
   start_ms=..., end_ms=...) around the remark to capture what the screen
   actually showed (frames + OCR).
3. search(job_id="$ARGUMENTS", query="<feature term>") to gather all mentions of
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
