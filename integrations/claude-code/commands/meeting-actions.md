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
2. Multi-person meeting without `speaker` labels on segments? Re-run
   process_media(path=<original file>, diarize=true, num_speakers=<attendee
   count when known>) — it adds S1/S2/… labels to the existing job without
   re-transcribing, and minutes with owners need them.
3. Attendees are listed above and the file is not processed yet (or you are
   re-running process_media anyway)? Pass vocabulary="<the attendees' names>"
   in that call — names survive transcription instead of degrading into
   look-alike words, and owner attribution depends on them.
4. When segments carry speaker labels, map each label to a person before
   writing minutes: self-introductions ("hi, this is Vera"), vocatives
   ("thanks, Tom"), and the attendees list above are the evidence. State the
   mapping first (e.g. `S1 = Vera, S2 = Tom, S3 = unidentified`) — never
   guess beyond the evidence.
5. Collect: action items (who committed to what), decisions (what was agreed),
   open questions (raised but unresolved). Keep exact quotes and t_ms for each.
6. search(job_id="$ARGUMENTS", query="<name or topic>") to trace scattered
   follow-ups on one topic before summarizing it.
7. If the job has video (a screen-share was recorded), attach visual evidence to
   items that reference the screen via get_moment(job_id="$ARGUMENTS",
   start_ms=..., end_ms=...).

## Output

Markdown with three sections (open with the speaker mapping line when the job
is diarized):

1. **Action items** — `- [ ] <action> — owner: <name or "unassigned">, due:
   <date or "unspecified">, evidence: "<quote>" (t_ms, t_wall when known)`.
2. **Decisions** — one bullet per decision with the deciding quote + timestamp.
3. **Open questions** — what needs an answer, who raised it, timestamp.

Owners and dates come ONLY from spoken words — a commitment voiced by a mapped
speaker counts ("I'll send it" spoken by S2 = Tom → owner: Tom); never infer
beyond that. When unclear, write "unassigned"/"unspecified". Write the minutes
in the meeting's language; quotes stay verbatim.
