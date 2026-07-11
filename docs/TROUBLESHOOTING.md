# Troubleshooting

Short answers to the failure modes people actually hit. If yours isn't here,
[open an issue](https://github.com/korovin-aa97/talkthrough-mcp/issues).

## First run is slow / downloads a lot

One-time setup costs, all cached afterwards:

- no system ffmpeg → `static-ffmpeg` fetches a bundled build (~80 MB);
- first transcription → whisper model download into `~/.cache/huggingface`
  (`small` ≈ 464 MB, `large-v3-turbo` ≈ 1.5 GB);
- first OCR → RapidOCR models (tens of MB).

After that, expect roughly 3× faster than real time on an Apple-Silicon CPU
with the default `small` model, OCR included (a 2-minute clip ≈ 40 s).
Re-processing the same file returns instantly from the job store.

## `pip install` says "No matching distribution found"

Your Python is older than 3.11 (macOS ships 3.9 as `/usr/bin/python3`), so
pip filters out every release and prints the confusing "from versions: none".
Fixes: use `uvx talkthrough-mcp` (uv picks a compatible Python by itself), or
create the venv from a modern interpreter, e.g. `python3.12 -m venv`.

## The server doesn't show up in my client

- Restart the client after editing its MCP config — most read it at startup.
- Check `uvx` is on the PATH the client uses: `uvx --version`.
- Run the server command from your config manually in a terminal — import and
  download errors print to stderr there.
- Healthy state: the client lists 7 tools, and `list_jobs()` returns `[]` on
  a fresh install.

## Processing a long recording times out my agent call

Pre-process outside the session, then query instantly:

```bash
talkthrough-mcp process ~/Videos/session.mov
```

The store is content-addressed, so the agent's later `process_media` on the
same file is an instant re-call, and `list_jobs()` finds the job.

## Wrong language detected / garbled transcript

- Check `language_probability` in the summary — a low value means the
  detector was fooled (silence or music at the start does this).
- Pin the language: `process_media(path, language="ru", force=true)`.
- Garbled non-English text on the default model: re-call with
  `model="large-v3-turbo"` and `force=true`.
- Domain jargon getting mangled: pass `vocabulary="Name1, Name2"` — it biases
  the decoder.

## OCR misses on-screen text

- Non-Latin scripts: set `TALKTHROUGH_OCR_LANG` (`ru`, `ja`, `ko`, `ar`, …)
  and re-process with `force=true`; the recognition model downloads once.
- Tiny or low-contrast print is best-effort by design — use
  `extract_frame(job_id, at_ms, crop=...)` to hand your model the
  native-resolution pixels instead.

## `t_wall` is null or looks wrong

- The recorder wrote no usable metadata — pass
  `recorded_at="2026-07-11T14:30:00+02:00"` (with `force=true` to re-anchor
  an existing job).
- macOS 26 ⌘⇧5 records via ReplayKit and omits the QuickTime creation-date
  tag; those recordings resolve from the container tag (`confidence:
  medium`, UTC). Pass `recorded_at=` when local-timezone precision matters.
- Every result carries `confidence` — it names the ladder rung that matched
  (see README → Wall-clock anchoring).

## Where is my data? How do I remove it?

Everything lives under `~/.talkthrough` (override with `TALKTHROUGH_HOME`):

- `talkthrough-mcp gc --keep-days 30` prunes old jobs; deleting the whole
  directory removes every job.
- Whisper models cache in `~/.cache/huggingface`; uvx environments are
  cleared with `uv cache clean`.

Nothing is written anywhere else, and there is no telemetry to opt out of.

## Windows

Best-effort but CI-smoked (lint + unit + a real CLI run). Quote paths with
spaces; the per-job lock degrades to a no-op — fine on a single-user machine.
Details: README → Windows.
