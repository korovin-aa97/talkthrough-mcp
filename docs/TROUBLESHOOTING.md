# Troubleshooting

Short answers to the failure modes people actually hit. If yours isn't here,
[open an issue](https://github.com/korovin-aa97/talkthrough-mcp/issues).

## First run is slow / downloads a lot

Cold setup has four separate stages; identify the failing stage before
changing caches or certificates:

1. `uvx` resolves the package and creates its isolated environment.
2. If no compatible interpreter exists, uv downloads a managed Python.
3. The first `process_media` downloads media/model assets that are still missing.
4. Warm processing uses the installed environment and local caches without
   runtime network access.

First-media assets can include:

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
environments. Set `SSL_CERT_FILE` before either uv setup or the first media
processing on a TLS-inspecting corporate network: managed Python, package,
static-ffmpeg, and model downloads all need the trusted CA.

A second run in the same warm environment does not redownload dependencies.
Warm processing is network-free and reuses model caches; re-processing the
same file returns instantly from the content-addressed job store. On an
Apple-Silicon CPU, processing with the default `small` model and OCR is
typically around 3× faster than real time (a 2-minute clip ≈ 40 s).

## Corporate networks: model downloads stall or fail TLS

The settings below fix one-time downloads on locked-down networks (warm runs
are offline and unaffected):

- **uv environment or managed Python download fails TLS** — use the OS trust
  store with `UV_SYSTEM_CERTS=true`, or point `SSL_CERT_FILE` at a PEM bundle
  containing the corporate CA. A diagnostic/preseed command isolates this
  stage before launching the server:

  ```bash
  uv python install 3.12
  ```

  `UV_PYTHON_INSTALL_MIRROR` is supported for an organization mirror of
  `python-build-standalone`; use it only when that mirror preserves uv's
  expected release path layout. It does not disable certificate validation.

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
Fixes: use `uvx --python ">=3.11,<3.14" talkthrough-mcp`, or
create the venv from a modern interpreter, e.g. `python3.12 -m venv`.

## `uvx` selects managed Python 3.10 and cannot resolve Talkthrough

An older launcher can fail with a resolver message such as:

```text
No solution found ... current Python version (3.10.x) does not satisfy Python>=3.11,<3.14
```

Inspect the selected interpreter with `uv python find`. If no compatible
managed Python is available, install one with `uv python install 3.12`; this
diagnostic manual launch pins 3.12 explicitly:

```bash
uvx --python "3.12" "talkthrough-mcp[diarization]"
```

Generated Talkthrough v0.3.2 client configs and the Claude plugin
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
uvx --refresh --python ">=3.11,<3.14" "talkthrough-mcp[diarization]" --help
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
uvx --refresh --python ">=3.11,<3.14" "talkthrough-mcp[diarization]" --help
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
server unpinned (`uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization]"`),
so a session started
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
`uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization]"` as the server
command (JSON configs: `"args": ["--python", ">=3.11,<3.14",
"talkthrough-mcp[diarization]"]`), restart the client, retry.

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

The same atomic contract covers a full `force=true` rebuild. If the stored job
has active or pending speaker identities, the rebuild must explicitly resolve
`diarize=true`; omitting or disabling diarization is rejected before probing or
staging (the refusal counts the saved and pending-review identities it
protects). On success every previous identity becomes pending-review evidence
against the fresh roster. A transcription, diarization, frame/OCR, validation,
or commit failure leaves the previous manifest and frames readable.

## A rebuild was interrupted (kill, power loss) and frames look wrong

A rebuild publishes in three renames — previous keyframes aside, new
keyframes in, new manifest in. A hard kill between the last two leaves the
old manifest indexing keyframes that are no longer on disk. Since 0.4.0 that
state is repaired automatically the next time the job is locked: the next
`process_media` call on the file, `label_speakers` on the job, or
`talkthrough-mcp gc` finishes the publication when the staged rebuild is
intact (`rolled_forward`) or restores the previous keyframes otherwise
(`rolled_back`), and the response says so in `recovery_note`. Nothing
needs to be deleted by hand, and `gc` never treats such a workspace as
litter.

Keyframe files that went missing for any other reason (a manual deletion,
a sync client) are reported instead of hidden: `process_media` answers with
`integrity_note` and `frames.missing_files`, and `get_frames` / `get_moment`
skip the missing files and carry `missing_frame_count`. Re-run
`process_media(path, force=true)` (plus `diarize=true` when the job has
speaker identities) to rebuild.

## `manifest.json` is unreadable (hand edit, sync conflict, corruption)

Since 0.4.0 `process_media` on that file — with or without `force=true` —
rebuilds the job from the source and keeps the unreadable file as
`manifest.json.damaged-<timestamp>` inside the job directory; the response
carries `manifest_recovery_note`. Identities saved in the damaged file are
not carried over automatically: inspect the backup and re-save them with
`label_speakers`. Deleting the job directory is never required.

## A forced rebuild fails the disk preflight

A forced rebuild of a video job keeps the previous keyframes on disk until
the new manifest is published, so the job directory temporarily peaks at
roughly twice the size of its `frames/` directory (plus the temporary
audio WAV). The preflight refuses up front when free space is below twice
the media size plus the current `frames/` size, instead of failing halfway
through the copy; free up space and retry.

## A legacy video has no OCR name candidates

Video jobs produced before 0.3.1 stored OCR as a flat line, so current name
candidate heuristics may have no useful line boundaries. The job is not corrupt
and is served without migration; transcript and saved identities remain intact.
The response-only `name_candidates_note` explains this case. Regenerate only if
the hints matter, using `force=true, diarize=true`; line-aware OCR is rebuilt and
all previous identities move to pending review rather than disappearing.

## `process_url` says the `[url]` extra is missing

YouTube ingestion needs `yt-dlp` (plus the bundled JavaScript components and
a Deno runtime, all from PyPI). Use
`uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization,url]"` as the
server command (JSON configs: `"args": ["--python", ">=3.11,<3.14",
"talkthrough-mcp[diarization,url]"]`), restart the client, retry. The
generated configs and the Claude plugin already carry both extras. Direct
`https://` links to media files work without the extra.

## `process_url` on a video page (Instagram, TikTok, Wikimedia Commons, …)

A URL that is not a media file is handed to yt-dlp's page reader (its site
extractors plus generic HTML5/HLS player detection), always without cookies
or logins. What to expect:

- **TikTok, Wikimedia Commons file pages, pages with a plain player** —
  verified on release day; other sites work as far as their yt-dlp
  extractor works anonymously. `origin.provider` names the extractor that
  handled the page and `origin.provider_id` its public id.
- **Vimeo** — with yt-dlp 2026.08 the Vimeo extractor only works logged-in
  ("The web client only works when logged-in"), so public Vimeo pages are
  refused with that reason; there is no anonymous path to offer.
- **Instagram** — anonymous access works for some public posts and is
  rate-limited quickly; a "bot check" / "sign-in" refusal means the site
  blocked anonymous access, and there is no workaround here (cookies are
  not supported). Download the post yourself and use `process_media`.
- **"the page contains N videos"** — carousels and playlists are refused;
  pass a link to one video.
- **"no video could be found"** — yt-dlp has no extractor for the site and
  found no player on the page; find the actual video URL.
- Sites change; a page that worked last week may need a newer yt-dlp
  (`uvx --refresh …`).

## `process_url` refuses the URL

The error names the reason; the common ones:

- **playlist / channel / search / feed URL** — pass one video URL
  (`watch?v=…`, `youtu.be/…`, `shorts/…`). A `list=` parameter on a
  `watch` URL is ignored and only that video is taken.
- **live stream, upcoming, or "has not finished processing"** — only
  completed recordings with a known duration are supported; retry later.
- **private, members-only, age-restricted, DRM, region** — talkthrough sends
  no cookies, logins or headers and does not bypass restrictions.
- **credentials in the URL, a non-443 port, plain `http://`, a private or
  link-local address, more than 5 redirects** — the destination gate refused
  to connect; only public `https://` hosts are downloaded, and every
  redirect hop is checked again.
- **"not supported media"** — the direct link answered with HTML/JSON (a
  login page, a download landing page) instead of a media file; find the
  actual file URL.
- **byte, duration or disk cap** — `TALKTHROUGH_MAX_DOWNLOAD_BYTES` (default
  2 GiB) and `TALKTHROUGH_MAX_SECONDS` (7200) apply before and during the
  download; free space is checked up front and while streaming.

Errors never echo the URL (query strings may carry signed tokens); the
`origin` block of the summary names the provider and the public video id or
host instead.

## Codex cancels `process_url` ("user cancelled MCP tool call")

`process_url` is honestly annotated as an open-world, non-idempotent tool
(it reaches the network). Interactive Codex asks before running it; a
non-interactive `codex exec` run (approval policy `never`) auto-cancels it
and the agent reports the cancellation. Approve that one tool in
`~/.codex/config.toml` (or project `.codex/config.toml`) — every other
talkthrough tool is local and unaffected:

```toml
[mcp_servers.talkthrough.tools.process_url]
approval_mode = "approve"
```

(`default_tools_approval_mode = "approve"` on the `[mcp_servers.talkthrough]`
table approves all of its tools at once.) Claude Code and other clients that
prompt on annotations behave the same way: the prompt is the feature, not a
bug — a download is the one thing the server does outside your machine.

## YouTube download fails or picks a poor format

`yt-dlp` needs a JavaScript runtime for some YouTube formats. The `[url]`
extra installs Deno from PyPI and talkthrough passes its path explicitly, so
nothing has to be installed by hand; if the runtime is missing anyway the
download still tries the formats that do not need it and the error says
which stage failed. Corporate TLS inspection affects this download like any
other — set `SSL_CERT_FILE` (see above). The chosen format caps video at
1080p (OCR gains nothing above it) and merges into `.mp4`, `.webm` or
`.mkv`; ffprobe, not the file name, decides what was actually downloaded.
Upgrading `yt-dlp` inside the tool environment (a fresh `uvx --refresh …`)
is the usual fix when YouTube changes something.

## Where does a downloaded source go, and how do I refresh it?

The file is kept as `jobs/<job_id>/source/<youtube-<id>|direct-<hash>>.<ext>`
inside the job (`extract_frame` decodes it later without network) and is
deleted with the job by `talkthrough-mcp gc`. The URL index under
`~/.talkthrough/urls/` maps a hashed key to the job id — no raw URL is
stored. A repeat `process_url` on the same URL serves the stored job with no
network; `refresh=true` downloads again, and if the bytes changed you get a
new job id. The same bytes reached through a local file and through a URL
are one job.

## `t_wall` is null or looks wrong

- The recorder wrote no usable metadata — pass
  `recorded_at="2026-07-11T14:30:00+02:00"` (with `force=true` to re-anchor
  an existing job).
- URL jobs never use the download time or the provider's upload date as a
  recording start (`origin.published_at` is reported separately), so
  `wall_clock` is null unless the container carries a creation tag or you
  pass `recorded_at=`.
- macOS 26 ⌘⇧5 records via ReplayKit and omits the QuickTime creation-date
  tag; those recordings resolve from the container tag (`confidence:
  medium`, UTC). Pass `recorded_at=` when local-timezone precision matters.
- Every result carries `confidence` — it names the ladder rung that matched
  (see README → Wall-clock anchoring).

## Where is my data? How do I remove it?

Everything lives under `~/.talkthrough` (override with `TALKTHROUGH_HOME`):

- `talkthrough-mcp gc --keep-days 30` prunes old jobs; deleting the whole
  directory removes every job.
- Whisper models cache in `~/.cache/huggingface`; `uv cache prune` is the
  ordinary cleanup for unused uv entries and cached tool environments.
- `uv cache clean talkthrough-mcp` removes that package's uv cache entries;
  bare `uv cache clean` clears the entire uv cache. Both can force package or
  tool-environment downloads again, so use them only for deliberate repair.
- `talkthrough-mcp gc --keep-days 30` cleans Talkthrough jobs and abandoned
  job staging (and first repairs any interrupted rebuild it finds); it does
  not clean uv environments, Python installs, or models.

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
