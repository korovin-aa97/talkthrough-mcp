# Recipe: file approved findings as GitHub issues

The triage flow (`triage-recording` → findings JSON per
[`../output-contract.schema.json`](../output-contract.schema.json) → the
narrator approves digest items) ends with structured findings. This recipe
turns the approved ones into GitHub issues with the `gh` CLI.

Prerequisites: `gh auth login` done, `jq` installed, findings saved to
`findings.json`.

## The loop

```bash
REPO="you/your-product"        # where the issues go
JOB="ab4dcf3f5acf435c"         # talkthrough job id (for the frames pointer)

# route=question findings go back to the narrator, never into the tracker;
# drop them (and anything the narrator rejected) before filing.
jq -c '.findings[] | select(.route != "question")' findings.json |
while read -r f; do
  title=$(jq -r '.title' <<<"$f")
  body=$(jq -r --arg job "$JOB" '
    "> " + .quote
    + "\n\n**When:** t=" + (.t_ms|tostring) + " ms"
    + (if .t_wall then " · " + .t_wall + " (wall clock — grep your logs ±30 s)" else "" end)
    + "\n\n**Observed:** " + .observed
    + "\n\n**Expected:** " + .expected
    + "\n\n**Acceptance criteria:**\n" + (.acceptance_criteria | map("- [ ] " + .) | join("\n"))
    + "\n\n**Verify via:** " + .verify_via
    + "\n\n**Evidence frames:** " + (if (.frame_refs|length) > 0
        then (.frame_refs | join(", ")) + " in `~/.talkthrough/jobs/" + $job + "/frames/`"
        else "none (audio-only)" end)
    + "\n\n_severity: " + .severity + " · route: " + .route
    + " · confidence: " + .confidence + "_"
  ' <<<"$f")
  gh issue create --repo "$REPO" --title "$title" --body "$body"
done
```

## Notes

- **Frames**: `gh issue create` cannot upload images. The recipe links the
  local frame paths; when the team needs to SEE them, either commit the
  handful of referenced frames to a branch and use raw URLs, or drag-drop
  them into the issue afterwards in the web UI.
- **Labels**: add `--label "bug"` / map `severity` to your label scheme with
  `--label "$(jq -r '.severity' <<<"$f")"` once those labels exist in the repo.
- **feature vs bug**: `route` is already the split — filter twice and send
  `route=="feature"` to your ideas tracker instead, if you keep them apart.
- **Idempotency**: re-running the loop files duplicates; filter against
  `gh issue list --search "<title>"` first if you need re-runs to be safe.
