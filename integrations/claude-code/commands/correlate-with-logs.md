---
description: "Walk a recording's remarks against system logs using wall-clock timestamps."
---

If no job_id was passed as an argument: call list_jobs() to find the right recording, or process_media(path) if the user gave a file path — then follow the workflow below with that job_id.

You are debugging with two evidence streams: a narrated recording (talkthrough
job `$ARGUMENTS`) and system logs the user can access. Pair spoken/visible moments
with log lines using wall-clock time.

## Method

1. Confirm the job's wall-clock anchor: list_jobs() shows wall_clock.start and
   its confidence. If wall_clock is null or confidence is "low", ask the user
   for the recording start time, then re-anchor with process_media(path=...,
   recorded_at="<ISO 8601>", force=true) before correlating.
2. Find the incident moments: search(job_id="$ARGUMENTS", query="<error term>")
   and/or get_transcript ranges. Every hit carries t_wall.
3. For each moment, compute the log window t_wall ± 30 s and read the user's
   logs there (ask the user to run the grep if you cannot access the log
   source directly).
4. Pull the matching visual state with get_moment(job_id="$ARGUMENTS",
   start_ms=..., end_ms=...) — the OCR text often contains the on-screen error
   that names the failing component.

## Output

A markdown incident walkthrough: one section per correlated moment with (a) the
narrator's quote + t_wall, (b) the matching log lines, (c) the frame ref showing
the screen, and (d) your read of cause vs. symptom. Close with a "Confidence and
gaps" note: which correlations are exact (t_wall confidence "exact"/"high") and
which are approximate ("medium"/"low" — mtime-derived anchors drift). Quote
remarks verbatim in their original language.
