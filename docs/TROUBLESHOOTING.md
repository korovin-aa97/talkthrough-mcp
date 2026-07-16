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

## Diarization finds the wrong number of speakers

- **Pass `num_speakers` first.** If the headcount is known, an exact k
  removes the failure mode entirely — unknown-count clustering is the
  fragile part, not the voice fingerprints.
- No headcount? Tune `TALKTHROUGH_DIARIZATION_THRESHOLD` (default `0.5`):
  **too few** speakers detected (voices merged) → **lower** it (try `0.4`);
  **too many** (one voice split) → **raise** it (try `0.6`). Re-run with
  `diarize=true` — an explicit request re-clusters the stored job in seconds
  without re-transcribing.
- Sub-second interjections ("yeah", "mhm") being absorbed into the other
  speaker's segment is expected at segment-level attribution — see README →
  Limitations.

## `diarize=true` fails with "[diarization]" in the error

The optional engine isn't installed. Use
`uvx "talkthrough-mcp[diarization]"` as the server command (JSON configs:
`"args": ["talkthrough-mcp[diarization]"]`), restart the client, retry.

If you installed into your own uv **project** (`uv add
"talkthrough-mcp[diarization]"`) and `import sherpa_onnx` fails with a
`libonnxruntime` dlopen error: sherpa-onnx 1.13.4's sdist metadata omits its
`sherpa-onnx-core` dependency, and uv's universal (lockfile) resolution
trusts the sdist — the package with the vendored ONNX Runtime silently never
installs. Add this override to your project's `pyproject.toml` and re-lock:

```toml
[[tool.uv.dependency-metadata]]
name = "sherpa-onnx"
version = "1.13.4"
requires-dist = ["sherpa-onnx-core==1.13.4"]
```

`uvx` and `pip` installs are unaffected (they read the wheel metadata).

## Diarization on an offline machine

Model downloads are one-time and pinned (URL + sha256); warm runs are
zero-network. To preseed a machine with no network at all: copy the two
`.onnx` files from a machine that has them
(`~/.talkthrough/models/diarization/<name>/model.onnx`) — or download the
pinned assets yourself — and point the env vars at the files:

```bash
TALKTHROUGH_DIARIZATION_SEG_MODEL=/models/segmentation.onnx
TALKTHROUGH_DIARIZATION_EMB_MODEL=/models/embedding.onnx
```

Paths are used verbatim, no network is touched.

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

Diarization caveat: sherpa-onnx vendors its own ONNX Runtime, but a stray
`onnxruntime.dll` in `C:\Windows\System32` (left there by some installers)
takes precedence in the DLL search order and can shadow the vendored one
with a version-mismatch crash (upstream k2-fsa/sherpa-onnx#3059). Fix:
remove/rename that stray DLL — it does not belong in System32.
