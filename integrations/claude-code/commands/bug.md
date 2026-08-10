---
description: "Turn one screen recording of a bug into an evidence-backed GitHub issue draft (quote, frames, OCR identifiers, wall-clock; silent recordings work too)."
---

If no job_id was passed as an argument: call list_jobs() to find the right recording, or process_media(path) if the user gave a file path — then follow the workflow below with that job_id.

You are turning a recorded bug into ONE evidence-backed GitHub issue draft.
The recording was processed by talkthrough as job `$ARGUMENTS`. Evidence comes
before everything else — and this is a bug report, not a fix: change no code.

## Method — evidence first

1. Orient. Short recording: get_transcript(job_id="$ARGUMENTS") and read it
   whole. Long recording: search(job_id="$ARGUMENTS", query="<distinctive
   word>") (error text, feature names) and read only the relevant ranges.
   If the job_id looks wrong, verify with list_jobs().
2. A silent recording (no narration) is a VALID input, not an error:
   get_transcript returns an empty transcript with a note (and list_jobs
   shows has_transcript: false), while frames and on-screen text are still
   indexed — orient with search (OCR hits) and get_frames across the
   timeline instead.
3. Pick ONE bug — the highest-confidence, highest-severity problem the
   evidence supports. Mention anything else in a single "Also observed" line
   at the end of the draft; do not investigate it.
4. Evidence bundle: get_moment(job_id="$ARGUMENTS", start_ms=<t0-2000>,
   end_ms=<t1+2000>) around the key remark or on-screen failure. Inspect at
   least one returned frame with your own eyes. Describe the observed state
   from the pixels and OCR text — never from imagination.
5. Small text unreadable (error codes, request IDs, on-screen log lines)?
   extract_frame(job_id="$ARGUMENTS", at_ms=<exact ms>, crop=<region>) for a
   native-resolution look before quoting it.

## Checkpoint — write this block before drafting

- Heard: the narrator's exact words + timestamp (or "silent recording").
- Saw: the concrete visible state + OCR identifiers (error text, codes, IDs).
- When: t_wall + its confidence (or "relative timestamp only").
- Expected: the expected behavior, as stated or implied by the recording.

## Optional — log correlation

Only when the user pointed you at logs you can actually read from here. Grep
the t_wall ± 30 s window; quote matching lines VERBATIM — never invent or
paraphrase a log line. For deeper multi-moment correlation, switch to the
correlate-with-logs prompt instead.

## Output — the issue draft

Return a markdown issue draft; do NOT create an issue anywhere:

- **Title** — one line, symptom first.
- **Observed** — what actually happened, from pixels + narration.
- **Expected** — what should have happened.
- **Reproduction steps** — numbered, exactly as the recording shows them.
- **Severity** — P1 (flow broken) | P2 (default) | P3 (polish), one line why.
- **Evidence** — quote + t_ms (+ t_wall when known), frame_refs (the frame
  files you actually looked at), OCR identifiers, the matching log line when
  one was found.

Rules: ambiguous evidence → STOP and ask your user ONE concrete question
instead of guessing. Copy t_wall values VERBATIM from the payload — never
compute them from t_ms yourself. This is not a code review: report what the
recording shows, not what the codebase might contain. Write the draft in the
recording's language unless the user asks otherwise; keep every quote
verbatim in its original language.
