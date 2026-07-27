# Silent recording → evidence-backed issue draft

End-to-end example for `/talkthrough:bug`: a **silent** screen recording of a
checkout bug (no narration — like Windows Game Bar clips, which don't capture
the microphone by default) goes in; an evidence-backed GitHub issue draft
comes out.

**[▶ Video: the same scenario, narrated (1:18)](https://youtu.be/nHfGfEiVdE8)**
— the same demo app processed live in Claude Code, this time with the user
talking over the recording; the spoken words end up quoted in the report.

**Transparent labeling: this is a scripted reproduction of a demo-app bug.**
The app is a single self-contained page (`checkout-demo.html`, a fake shop —
not a real store), the reproduction is driven by Playwright
(`record_repro.py`), and the recording is a real capture of that run, saved
with no audio track. Nothing else about the flow is staged: the recording you
see is the file that was processed, and the issue draft below is the
unedited agent output.

## The bug

Apply coupon `SAVE20` on a €100.00 cart → total becomes €80.00 → press
**Pay** → red banner: **“Payment failed · Reference: req_7F3A”**. The on-page
dev log shows why: the front-end sends `{"amount": 80, "currency": "eur"}` —
euros where the gateway expects **cents** — and the gateway rejects 80 cents
as below its 100-cent minimum (`402 amount_too_small`). The fix would be
`Math.round(total * 100)`.

## Files

| File | What it is |
|---|---|
| [`checkout-demo.html`](checkout-demo.html) | The buggy one-page checkout app (self-contained, no network) |
| [`record_repro.py`](record_repro.py) | Playwright script that reproduces the bug and records it (~18 s, silent) |
| [`checkout-coupon-bug.mp4`](checkout-coupon-bug.mp4) | The actual recording — video only, no audio track |
| [`issue-draft.md`](issue-draft.md) | Unedited `/talkthrough:bug` output for that recording (claude sonnet, headless run of the shipped command text) |

## Reproduce it yourself

```bash
pip install playwright && playwright install chromium
python record_repro.py                      # → checkout-coupon-bug.mp4
```

Then, in Claude Code with the [talkthrough plugin](../../README.md#claude-code)
installed:

```
/talkthrough:bug examples/bug-from-silent-recording/checkout-coupon-bug.mp4
```

Any other MCP client: invoke the `bug` server prompt with the processed
job id, or paste [`examples/prompts/bug.md`](../prompts/bug.md).

## Why a silent recording, on purpose

Because that's the hard case. With no narration there is no transcript to
search — the agent works from keyframes and OCR'd on-screen text alone: the
€100.00 → €80.00 flip, the red banner, the `req_7F3A` reference, and the dev
log's `402 amount_too_small` line are all read off the pixels. Narrated
recordings only add signal on top (a searchable transcript with wall-clock
timestamps).
