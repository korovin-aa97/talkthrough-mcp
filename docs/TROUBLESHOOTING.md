# Troubleshooting

Short answers to the failure modes people actually hit. If yours isn't here,
[open an issue](https://github.com/korovin-aa97/talkthrough-mcp/issues).

## First run is slow / downloads a lot

There are two separate cold-start stages. First, `uvx` resolves a compatible
Python and creates an environment for the requested package version. Then the
first `process_media` downloads media/model assets that are still missing:

- no system ffmpeg → `static-ffmpeg` fetches a bundled build (~80 MB);
- first transcription → whisper model download into `~/.cache/huggingface`
  (`small` ≈ 464 MB, `large-v3-turbo` ≈ 1.5 GB);
- first OCR → RapidOCR models (tens of MB);
- first diarization → the pinned segmentation and embedding models.

A pinned plugin version and its selected interpreter form a distinct `uvx`
environment. After a plugin update, a machine without system ffmpeg may
therefore fetch the ~80 MB static build again; one external cold run took
about 50 seconds, but that is an observation, not a timing promise. The
Whisper, OCR, and diarization model caches are shared across those tool
environments. Set `SSL_CERT_FILE` in the MCP server environment before the
first media processing on a TLS-inspecting corporate network, because the
static-ffmpeg download needs the same trusted CA as model downloads.

A second run in the same warm environment does not redownload dependencies.
Warm processing is network-free and reuses model caches; re-processing the
same file returns instantly from the content-addressed job store. On an
Apple-Silicon CPU, processing with the default `small` model and OCR is
typically around 3× faster than real time (a 2-minute clip ≈ 40 s).

## Corporate networks: model downloads stall or fail TLS

Two env vars fix the one-time downloads on locked-down networks (warm runs
are offline and unaffected):

- **Whisper download hangs / stalls at 0%** — some proxies break Hugging
  Face's Xet transfer protocol. Disable it; downloads fall back to plain
  HTTPS:

  ```bash
  HF_HUB_DISABLE_XET=1
  ```

- **Diarization model download fails with a certificate error** — TLS
  inspection re-signs traffic with a corporate CA that Python's bundled
  certificate store doesn't trust. Point Python at the system store that
  includes your CA (macOS: `/etc/ssl/cert.pem`; Linux distros commonly use
  `/etc/ssl/certs/ca-certificates.crt`):

  ```bash
  SSL_CERT_FILE=/etc/ssl/cert.pem
  ```

  On a machine without system ffmpeg, the first cold-start download may
  instead come from the third-party `static-ffmpeg` package. Its raw
  certificate error has the same cause, and the same `SSL_CERT_FILE` setting
  applies to that one-time ffmpeg download too.

Set both in the MCP server config (`"env": {...}`) or the shell that runs
the first `process_media`. Once the models are cached, neither is needed —
processing is zero-network by design. Since 0.2.6 the diarization download
error itself names this fix when the failure looks like TLS interception.

The same applies to running the test suite on such networks: the
diarization integration tests download models on first run, so a fresh
`uv run pytest tests/integration` can fail with certificate errors until
`SSL_CERT_FILE` is set (the rest of the suite is unaffected).

## `pip install` says "No matching distribution found"

Your Python is older than 3.11 (macOS ships 3.9 as `/usr/bin/python3`), so
pip filters out every release and prints the confusing "from versions: none".
Fixes: use `uvx talkthrough-mcp` (uv picks a compatible Python by itself), or
create the venv from a modern interpreter, e.g. `python3.12 -m venv`.

## `uvx` selects managed Python 3.10 and cannot resolve Talkthrough

An older launcher can fail with a resolver message such as:

```text
No solution found ... current Python version (3.10.x) does not satisfy Python>=3.11,<3.14
```

Inspect the selected interpreter with `uv python find`. If no compatible
managed Python is available, install one with `uv python install 3.12`; a
manual launch can select it explicitly:

```bash
uvx --python 3.12 "talkthrough-mcp[diarization]"
```

Generated Talkthrough v0.3.1 client configs and the Claude plugin already
pass the canonical `>=3.11,<3.14` range from `pyproject.toml`, so uv selects
or downloads a compatible interpreter instead of inheriting Python 3.10.

## The server doesn't show up in my client

- Restart the client after editing its MCP config — most read it at startup.
- Check `uvx` is on the PATH the client uses: `uvx --version`.
- Run the server command from your config manually in a terminal — import and
  download errors print to stderr there.
- Healthy state: the client lists 8 tools, and `list_jobs()` returns `[]` on
  a fresh install.

## Claude Code says `Failed to reconnect … -32000` / `No module named 'mcp.server.fastmcp'`

Talkthrough **0.3.0 and newer** use the MCP Python SDK 2.x public API
(`mcp>=2.1.1,<3`) and import `mcp.server.mcpserver`; the old `mcp<2`
workaround must be removed. `uvx --with 'mcp<2' ...` adds a real dependency
constraint rather than overriding the package metadata, so using it with
0.3.0 fails resolution as unsatisfiable. Refresh the tool environment instead:

```bash
uvx --refresh "talkthrough-mcp[diarization]" --help
```

The history below applies only to older Talkthrough releases.

The server process died on import before the MCP handshake. On any
talkthrough-mcp **≤ 0.2.4** resolved after 2026-07-28 the cause is the MCP
Python SDK: its 2.0.0 release removed the `mcp.server.fastmcp` module the
server imports, and those talkthrough versions declared `mcp>=1.28.1` with no
upper bound, so a fresh resolve picked the incompatible SDK. Machines with a
warm pre-2.0 uv cache kept working until uv re-resolved — which made the
breakage look intermittent and machine-specific. It is neither.

Fixed for the 0.2.x line in **0.2.5** (`mcp>=1.28.1,<2`). Unpinned `uvx`
setups picked that bound up on their next resolve; the historical refresh was:

```bash
uvx --refresh "talkthrough-mcp[diarization]" --help
```

then reconnect (`/mcp`). The interim `"--with", "mcp<2"` launch argument is
compatible only with the old 0.2.x dependency line and must not remain in a
0.3.0 config.

## Updated the plugin, but the server behaves like the old version

A running session keeps its MCP server process alive: `claude plugin update
talkthrough@talkthrough` (or editing the version in any client's config)
updates the files immediately, but the already-spawned server keeps serving
the OLD version until the session/client restarts. Restart the session (or the
client) after updating; headless and CI runs pick up the new version on
their next launch because they spawn a fresh server every time. To verify
what is actually running, check `tool_versions` in any `process_media`
summary or manifest. Note the split (0.2.6+): `tool_versions` names what
*transcribed* the job and is deliberately never re-stamped by a diarization
amend; `diarization.produced_by` names the version that wrote the *current
speaker labels* — after an amend the two can legitimately differ.

The update command needs the marketplace-qualified id — plain `claude plugin
update talkthrough` fails with `Plugin "talkthrough" not found`. `claude
plugin list` prints the id to use.

## The plugin says 0.2.3 but the server reports a newer version

Also expected, and the reverse of the case above. The plugin launches the
server unpinned (`uvx talkthrough-mcp[diarization]`), so a session started
after a PyPI release resolves the NEWEST server even while the plugin — the
slash commands, the skill, the triage agent — is still on your installed
version. Sessions started before the release keep serving the older one in
parallel, so two sessions on one machine can disagree.

This is harmless for additive releases (a new server prompt simply has no
matching slash command yet) and it is why server changes stay compatible with
the previous release's command pack. `claude plugin update
talkthrough@talkthrough` + a restart re-aligns both halves; `tool_versions`
tells you what the server actually is.

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
- Budget note: an explicit model change reprocesses the WHOLE job (STT +
  frames + OCR + diarization), not just the transcript — plan for up to half
  the recording's own duration on a laptop (measured: a 65-minute 1080p
  meeting took 28.5 minutes). A diarization-only change amends without
  re-transcribing, but the diarization stage itself still re-scans the full
  audio — minutes on long recordings (measured: ~12 minutes on a large
  meeting), not seconds.

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
- `num_speakers` is a **target, not a guarantee**: the clusterer can
  converge on fewer clusters than the k you passed. Since 0.2.6 a re-run
  that changed nothing says so instead of reporting plain success — look for
  `labels_changed: false` and the "nothing was relabelled" note.
- No headcount? Tune `TALKTHROUGH_DIARIZATION_THRESHOLD` (default `0.5`):
  **too few** speakers detected (voices merged) → **lower** it (try `0.4`);
  **too many** (one voice split) → **raise** it (try `0.6`), then re-process
  with `force=true`. A threshold change alone does not invalidate the stored
  labels (only an explicit `num_speakers` change or a failed previous run
  triggers the cheap diarization-only amend), and a `force=true` re-run
  redoes the whole pipeline — budget accordingly (see the note above).
- Sub-second interjections ("yeah", "mhm") can still be absorbed when the
  diarization engine does not emit a separate turn; word-level attribution
  cannot recover a turn the engine never detected — see README → Limitations.

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

## A failed re-diarize keeps the stored labels

An explicit `diarize=true` amend that fails returns an error and leaves the
stored job byte-identical: transcript, active speaker names, and pending-review
evidence all survive. Since 0.2.3 this applied to failures while constructing
the engine (a mistyped `TALKTHROUGH_DIARIZATION_*_MODEL`, a dead model
download); 0.3.1 extends the same fail-safe contract to failures inside the
diarization run. Correct the environment and retry the amend.

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

CI-smoked on every push (lint + unit + a real CLI run + a diarize smoke).
Quote paths with spaces; the per-job lock degrades to a no-op — fine on a
single-user machine. Details: README → Windows.

Diarization caveat: sherpa-onnx vendors its own ONNX Runtime, but a stray
`onnxruntime.dll` in `C:\Windows\System32` (left there by some installers)
takes precedence in the DLL search order and can shadow the vendored one
with a version-mismatch crash (upstream k2-fsa/sherpa-onnx#3059). Fix:
remove/rename that stray DLL — it does not belong in System32.
