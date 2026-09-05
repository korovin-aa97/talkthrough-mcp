# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.4.1] — 2026-09-05

External findings against 0.4.0 — a negative release QA over the published
wheel and a corpus of 40+ live URLs — all reproduced first (the log leak
offline, with `httpx.MockTransport`). No new tools; the wire contract is
unchanged: 9 tools, 6 prompts.

### Fixed

- **A request URL no longer reaches a log line.** httpx logs every request
  line at INFO with the full URL — one line per hop, redirect targets
  included — and the CLI enabled INFO on the root logger before dispatching
  any command, `serve` included. A direct link's query and a CDN's signed
  redirect target therefore landed in stderr, which MCP clients keep as log
  files, against the 0.4.0 promise. The CLI now holds the HTTP client
  loggers at WARNING and passes every foreign log record and every
  traceback through `url_ingest.redact`; a canary test with real httpx
  records over a redirect pins it.
- **A multi-video page is refused, not silently truncated.** The page probe
  asked yt-dlp for playlist item 1 only, so the entry count it checked was
  always 1: a Loom folder or an Instagram carousel was ingested as its first
  video. The probe now reads a flat entry list, counts, and refuses with
  `the page contains N videos — pass a link to one video` before any
  download instance exists.
- **`refresh=true` replaces the stored provider metadata.** When the
  refreshed bytes were unchanged the job was served with its old
  `media.origin`: the summary said `refreshed: true` next to a stale title
  and `downloaded_at` while the URL index entry was rewritten. The block is
  now replaced under the job lock; ordinary convergence (a local file
  first, then a URL with the same bytes) stays additive as documented.
- **`gc` sweeps orphan URL lock files.** One empty `urls/<key>.lock` per URL
  was created before validation and never removed, so refused and failed
  URLs grew the index directory forever. `gc` unlinks every lock without a
  mapping while holding it and reports them as `urls/<key>.lock`;
  `url_lock` re-checks the inode after acquiring, so a waiter never ends up
  holding a lock on a file the sweep removed.
- **Page-reader crashes say what to do.** An exception the extractor stack
  raised past yt-dlp's own error class (yt-dlp's TED extractor on a changed
  page: a bare `TypeError`) now reads `the page reader failed on
  https://host/… (TypeError: …) — the site may have changed its page
  layout: refresh the tool environment so yt-dlp updates, or pass a direct
  link to the media file`, on both the YouTube and the page path.

### Added

- **`talkthrough-mcp --version`** prints the package version, the Python it
  runs on and the state of the optional extras (`url extra: yt-dlp <v>` or
  `not installed (direct https:// media links only)`; `diarization extra:
  sherpa-onnx <v>`). The server logs the same line to stderr at every
  start, so a client's MCP log shows which server and which extras
  answered — a hand-written `uvx talkthrough-mcp` config upgrades in place
  and lists `process_url` without being able to read YouTube or video
  pages. CI runs it in the version matrix and on the clean-installed wheel.
- **`--json` on failure** leaves one JSON document on stdout,
  `{"error": {"type": "UnsupportedUrlError", "message": "…"}}`, next to the
  unchanged exit code 2 and the human `error:` line on stderr, so
  automation parses one format on both outcomes.
- [`docs/URL_ACCEPTANCE_CORPUS.md`](docs/URL_ACCEPTANCE_CORPUS.md): the live
  URL corpus behind the release QA (full runs, cap and refusal paths,
  provider failures, negative cases, the 0.4.1 regressions) for manual
  regression runs — sites change, CI stays offline.

### Changed

- README documents the memory envelope of a cold run and how to upgrade a
  hand-written 0.3.x config; TROUBLESHOOTING points at `--version` where
  it asks which server is actually running.

## [0.4.0] — 2026-09-05

One theme: **give Talkthrough a public video URL and keep everything else
local**. The source is downloaded once and kept with the job; transcription,
frames, OCR, search and every later question stay on the machine.

### Added

- **`process_url` is the ninth MCP tool and the server's only network
  tool.** It accepts a direct `https://` link to a media file, one public
  YouTube video (`watch`, `youtu.be`, `shorts`, `live`, `embed`; playlist
  parameters are dropped), or any public video page yt-dlp can read
  (verified on release day: public Instagram reels, TikTok, Wikimedia
  Commons file pages; yt-dlp's other extractors and generic HTML5/HLS
  player detection are used as-is — no cookies or logins, ever, so a site
  that demands a sign-in for anonymous clients, as Vimeo does with this
  yt-dlp, is refused with the reason). It downloads the source
  once into a private workspace under hard caps, verifies the bytes with
  ffprobe, installs the file inside the job as a Talkthrough-named managed
  source (`youtube-<id>`, `<provider>-<id>` or `direct-<hash>`) and runs the
  same local pipeline `process_media` runs. Tool annotations say so:
  `open_world_hint=true`, `idempotent_hint=false`. `force=true` rebuilds a
  stored URL job from its kept source (re-anchor `recorded_at`, change the
  model) without a download. The CLI gains `talkthrough-mcp process-url
  <url> [--refresh] [--force] [--json]`.
- **URL index without URLs.** A hashed key (`youtube:<id>`, the SHA-256 of
  the exact URL, and for page videos `site:<extractor>:<id>`) maps to the
  job id, so a repeat call on the same URL — or another URL form of the same
  page video — serves the stored job with no download unless
  `refresh=true`. Job ids stay content hashes: the same bytes reached through
  two URLs, or processed earlier from a local file, converge on one job.
  Concurrent calls on one URL download once, and two URLs with the same
  bytes never destroy each other's download.
- **A destination gate for every hop.** Only `https://` on port 443, no
  credentials in the URL, and every DNS answer must be a public address
  (private, loopback, link-local/cloud-metadata, multicast, reserved, shared
  address space and IPv4-mapped forms are refused). The direct downloader
  pins the connection to the checked address (SNI and `Host` carry the name),
  tries every validated address on a connect failure, and re-validates each
  of at most five redirects. For video pages the named host is gated before
  yt-dlp's own client follows embedded players — a documented residual.
- **Caps enforced before and during the transfer.** Bytes
  (`TALKTHROUGH_MAX_DOWNLOAD_BYTES`, default 2 GiB, checked per chunk even
  without `Content-Length`), duration (`TALKTHROUGH_MAX_SECONDS` against
  provider metadata and again against ffprobe), free disk, redirect count and
  wall time; a failed or aborted download leaves no `.part` file, no job and
  no index entry.
- **Optional `[url]` extra.** `yt-dlp[default,deno]` brings yt-dlp, its
  bundled JavaScript components and a PyPI-distributed Deno runtime, so one
  install command covers YouTube and every other video page. yt-dlp runs with
  an allowlisted option set: no user configuration, no plugins, no cookies or
  logins, no remote JavaScript components, one video (carousels and
  playlists refused), no live streams, Talkthrough-owned output names, a
  deterministic merge, the resolved ffmpeg. Direct HTTPS links need no
  extra. Generated client configs and the Claude plugin install
  `[diarization,url]`; `uvx talkthrough-mcp` remains the minimal server.
  Non-interactive Codex cancels the open-world tool by default; the Codex
  integration page documents the per-tool approval override.
- **Provider facts, not secrets, in the manifest.** `media.origin` stores the
  provider, public video id or host, a one-way URL hash, a bounded title,
  the provider's publication time, the downloader and the byte count;
  `media.managed_source` points at the kept file. The raw URL, its query and
  userinfo never reach a manifest, the index, a log line, a progress message
  or an error. `extract_frame` decodes the kept source; `list_jobs` shows
  the origin.
- `TALKTHROUGH_MAX_DOWNLOAD_BYTES` is documented in the MCP Registry
  manifest and every integration adapter.

### Changed

- **A download time is not a recording time.** URL-origin jobs skip the
  mtime rung of the wall-clock ladder, and the provider's upload date is
  reported as `origin.published_at`, never as `t_wall`; the summary says to
  pass `recorded_at` when a real start time is known.
- `gc` also removes URL index entries whose job is gone and download
  workspaces a dead process left behind; a managed source is deleted with
  its job.
- Guidance, the cross-engine skill, the example agent, prompts, generated
  integrations, the MCP Registry manifest and the wire-contract tests now
  agree on **9 tools and 6 prompts**. The MCP Registry manifest moves from
  the deprecated `2025-09-29` schema to `2025-12-11`.
- The privacy statement is more precise: nothing is ever uploaded, local
  files never trigger runtime network access, and `process_url` is the one
  explicit download.

### Fixed

External findings against 0.3.2, all reproduced first:

- **An interrupted rebuild no longer leaves a job silently inconsistent.** A
  hard kill between the frame swap and the manifest publication of a forced
  rebuild left the old manifest indexing keyframes that were no longer on
  disk; `process_media` reported a clean cache hit, `get_frames` served
  fewer images than promised and `gc` deleted the backup after 24 h. The job
  lock now finishes such a commit when the staged rebuild is intact
  (`rolled_forward`) or restores the previous keyframes (`rolled_back`) — in
  `process_media`, `label_speakers` and `gc` — and says so in
  `recovery_note`. Read tools compare the frame index with the directory and
  report `missing_frame_count` plus an `integrity_note` instead of serving
  fewer frames silently.
- **An unreadable `manifest.json` is quarantined, not a crash.** `force=true`
  (and a plain call) on a job whose manifest had become unreadable answered
  with a bare "Error executing tool"; the rebuild now keeps the file as
  `manifest.json.damaged-<timestamp>` beside the fresh one and reports
  `manifest_recovery_note`. Unexpected exceptions in any tool now reach the
  agent with their type and message.
- **The rebuild disk preflight reserves the existing keyframes.** A forced
  rebuild keeps the previous frames on disk until it commits, so the job
  directory peaks at roughly twice its `frames/` size; the preflight now
  accounts for it instead of failing halfway.
- **The name-candidate filter rejects UI chrome by vocabulary.** It used to
  reject exactly the twelve strings a review had listed and accept their
  neighbours (Screen Sharing, Raise Hand, Speaker Notes, …). It now carries
  a vocabulary of meeting-app, recorder, IDE, browser and dashboard chrome
  (English, Russian, a few DE/ES/FR terms) and rejects a phrase when every
  word, or a majority of at least two words, is chrome; a name plate that
  merely carries a role survives. Common given names and surnames are
  deliberately absent; a corpus test pins both sides.
- Pending-review entries beyond the response cap are listed by label in
  `speaker_names_pending_review_hidden_labels` (capped at 100), so they can
  be removed without guessing.
- The force refusal on an identity-bearing job counts saved and
  pending-review identities separately instead of calling stale pending
  entries "saved speaker identities".
- The response-only `speaker_names_pending_review_dropped` report is now
  documented as reachable only for hand-edited or foreign manifests.
- Internal Russian planning documents were removed from the public tree.

### Compatibility

- Existing 0.1.x–0.3.2 manifests load in place without migration;
  `media.origin` and `media.managed_source` are additive and absent on
  local-file jobs, which serialize byte-identically to before.
- `process_media` remains local-only. Tool and prompt counts are 9/6.
- The `[url]` extra is optional; without it, YouTube URLs return an
  actionable install hint and direct HTTPS links still work.

## [0.3.2] — 2026-09-03

A data-safety and portability patch from externally reproduced reports. No
tools, prompts, dependencies, or manifest schema versions were added.

### Fixed

- **Pending speaker identities are lossless across repeated relabels.** Active
  and already-pending names now merge deterministically instead of replacing
  each other. Every retained item carries bounded old-roster context; current
  labels can be confirmed, replaced, or removed, stale labels can be removed
  explicitly, and superseded collisions are reported once instead of being
  silently discarded.
- **Full forced reprocessing is transactional and identity-safe.** A job is
  rebuilt in a hidden same-filesystem staging directory, validated, and
  committed manifest-last; STT, diarization, OCR, validation, or commit
  failures keep the previous manifest and frames intact. A named job refuses
  a full rebuild unless diarization is resolved on, and successful rebuilds
  move every previous identity to pending review against the new roster.
- **Legacy OCR limitations are explicit.** Video jobs produced before 0.3.1
  with flat OCR remain readable without migration and now explain why name
  candidates may be absent and how to regenerate safely. The candidate filter
  accepts broader real-world names, particles, apostrophes, lowercase scripts,
  and trailing role metadata while rejecting known UI chrome; hints remain
  capped, deduplicated, and never become identities automatically.
- **Every agent-facing `uvx` launcher selects supported Python portably.** The
  canonical argv is `--python`, `>=3.11,<3.14`, and the package spec. Shell
  prose uses portable double quotes, while JSON, TOML, deeplinks, and plugin
  arguments retain raw argv values. A generator-owned quoting allowlist and
  repository-wide semantic audit prevent shell redirection regressions.
- **Supported interpreters are continuously exercised.** CI now runs frozen
  unit/import/CLI/MCP inventory checks on Python 3.11, 3.12, and 3.13 in
  addition to the existing Linux, macOS, and Windows gates. Native bash, sh,
  zsh, PowerShell, and cmd launcher smokes verify the same 8-tool/6-prompt
  server contract.
- **Cold-start, TLS, and cache recovery instructions name the actual stages.**
  Documentation separates uv environment resolution, managed Python, media
  assets, and warm offline processing; covers `SSL_CERT_FILE`,
  `UV_SYSTEM_CERTS`, `UV_PYTHON_INSTALL_MIRROR`, targeted prune/clean commands,
  and the distinct Talkthrough job GC scope.
- **GitHub Release notes are deterministic.** The release workflow extracts
  this exact changelog section and fails before publishing on a missing,
  duplicate, empty, or tag-mismatched section. Documentation tests now assert
  operational facts instead of pinning incidental marketing prose.
- **Cross-segment evidence no longer renders a doubled crop marker.** Two
  independently cropped quote windows share one `…` at their boundary while
  preserving search semantics and the 240-character cap.

### Compatibility

- Existing 0.1.x–0.3.1 manifests load in place without migration or rewrite;
  pre-0.3.1 flat OCR remains unchanged and is described by a response-only
  compatibility note.
- Full force reprocessing of a job with saved or pending speaker identities now
  requires diarization and changes those identities from active claims to
  pending-review evidence. Unnamed jobs retain the previous force behavior.
- OCR text produced by current versions keeps embedded newlines between
  detected boxes; search whitespace behavior and bounded response shapes are
  unchanged.
- Progress remains the first MCP notification and no deprecated logging
  capability is reintroduced. A failed diarization amend or full force returns
  an error and preserves the old stored job rather than persisting an
  `available=false` replacement.
- The root development `.mcp.json` remains a local `uv run --directory`
  configuration. Generated distribution launchers and the released Claude
  plugin use the supported Python selector; tool and prompt counts remain 8/6.

## [0.3.1] — 2026-09-02

A compatibility-and-honesty patch from six externally reproduced findings.
No tools, prompts, dependencies, or manifest schema versions were added.

### Fixed

- **Verified speaker names survive a label-changing diarization amend for
  review.** Active names and their evidence move atomically into bounded
  `speaker_names_pending_review` fields instead of disappearing or being
  attached to unproven new labels. They remain inactive in search, text, SRT,
  segments, and the roster until an explicit `label_speakers` review; failed
  amendments leave the stored manifest byte-identical.
- **Generated `uvx` launchers select a supported Python.** Every distribution
  config now prepends `--python ">=3.11,<3.14"`, sourced directly from
  `pyproject.toml`; released Claude plugin packs still pin their exact matching
  server (`talkthrough-mcp[diarization]==0.3.1`). Shell snippets quote the
  range and extras safely, while JSON/TOML/deeplinks keep raw argv values.
- **Long cross-segment search hints keep evidence from both sides.** The
  bounded quote is assembled around matched tokens in both adjacent segments
  with Unicode/`ё→е` search normalization and word-boundary ellipses. If an
  exceptional anchor set cannot fit, the payload calls it a sample instead of
  claiming every matched token is visible.
- **OCR name candidates are production-shaped and more selective.** RapidOCR
  box boundaries are retained as newline-separated text; search keeps its
  whitespace semantics. Candidate hints are capped at three strings of at
  most 80 characters and reject digits, URLs/paths, menu chrome, excessive
  punctuation, and long UI copy while retaining name-like cased and uncased
  scripts. They are still never promoted to speaker identities automatically.
- **MCP SDK 2.x no longer requests the deprecated logging capability.** The
  former `ctx.info` message is now the first progress notification; structured
  output, annotations, CLI behavior, eight tools, and six prompts are
  unchanged.
- **Cold-start documentation distinguishes environment resolution from media
  processing.** A new pinned plugin/interpreter environment can fetch the
  bundled ~80 MB ffmpeg again when no system ffmpeg exists, while shared
  Whisper/OCR/diarization caches and warm network-free processing remain
  reusable. Corporate TLS setup now explicitly precedes first processing.

### Compatibility

- Existing 0.1.x–0.3.0 manifests load without migration; the new pending-name
  fields are additive and omitted when empty.
- The root checkout `.mcp.json` remains a local `uv run --directory` config;
  only generated distribution launchers carry the Python selector.

## [0.3.0] — 2026-08-28

One theme: **verified speaker identity can survive beyond one agent turn**.
Talkthrough now attributes diarized speech at word boundaries, lets an agent
persist evidence-backed names without replacing the canonical `S1`/`S2`
labels, and serves those names consistently on every transcript surface.

### Added

- **`label_speakers` is the eighth MCP tool.** It atomically stores or removes
  verified `S1 → name` mappings under the job lock, with optional bounded
  evidence. Raw labels stay canonical; saved names are additive and are
  returned separately by transcript segments, text/SRT output, search hits,
  moments, rosters, summaries, and job listings.
- **Word-level speaker attribution.** New diarized jobs retain bounded Whisper
  word timings and split a transcript segment when the dominant voice changes
  between words. The committed interrupt fixture covers a mid-sentence handoff;
  old manifests continue to report `attribution_precision="segment"` without
  a hidden re-transcription.
- **Bounded OCR name candidates.** Diarization summaries expose possible
  on-screen names near useful speaker anchors as hints only. Candidates are
  never promoted to saved names without an explicit `label_speakers` call.
- **Broader lexical retrieval.** `search` adds `match_mode="any_word"`, folds
  Unicode punctuation and Russian `ё/е`, and matches phrases that straddle two
  adjacent transcript segments while keeping bounded, token-safe results.
- `list_jobs` now includes the stored local media path, and speaker rosters
  expose honest long-turn start and duration fields.

### Changed

- **MCP Python SDK 2.x.** Runtime and E2E clients now use the public SDK 2.1.1
  snake-case contract (`MCPServer`, structured output, image blocks,
  annotations, progress, and errors). The dependency is `mcp>=2.1.1,<3`; the
  emergency pre-2.0 workaround from 0.2.5 is no longer needed.
- Released Claude Code plugin packs now pin their matching server exactly
  (`talkthrough-mcp[diarization]==0.3.0`). Ordinary install snippets remain on
  the latest public package.
- Guidance, the cross-engine skill, prompts, examples, generated integrations,
  MCP Registry manifest, and wire-contract tests now agree on **8 tools and 6
  prompts**.

### Fixed

- The Claude Code plugin's `feedback-triage` agent now whitelists the
  plugin-qualified MCP tool names, so it can actually call all eight tools
  instead of describing calls it cannot make.
- Frame dedup no longer collapses visually different solid-color screens that
  share the same dHash gradient: duplicate decisions also compare mean
  grayscale brightness.
- The misleading `longest_turn_ms` field now has explicit
  `longest_turn_at_ms` and `longest_turn_duration_ms` replacements; the old
  start-time alias remains for one compatibility cycle.
- No-op diarization amendments explain whether the unchanged labels came from
  a requested speaker count, an embedding-model change, or both.
- `gc` reports an old manifest-less directory once as swept instead of warning
  about the same directory immediately before removing it.
- Corporate TLS troubleshooting now covers the first `static-ffmpeg` download
  as well as diarization model downloads.
- If an optional non-Latin OCR pack cannot be downloaded, OCR now falls back
  to the bundled default pack instead of dropping all on-screen text.
- Early exhaustion of the frame-analysis cap is reported honestly instead of
  implying the full recording was inspected.

### Compatibility and validation

- 0.1.x/0.2.x manifests remain readable in place; wire and data changes are
  additive, including compatibility with the 0.2.6 Claude command pack.
- The release battery ran 210 isolated agent cells across six Claude/Codex
  configurations. All 102 judged full-grid cells were audited, all 30 v0.3
  behavior cells passed mechanically, and old-server control resolved the only
  parity flip: **release-caused regressions = 0**.
- Production-like release-candidate acceptance passed 10 Claude Code plugin
  scenarios (all six commands, skill, agent, saved-name continuity, and the
  0.2.6 adapter) plus a Goose 1.41.0 client smoke.

## [0.2.6] — 2026-08-10

One theme: **the payload tells the truth about the outcome, not the
attempt** — the same line 0.2.2 (amend honesty) and 0.2.3 (fail-fast,
`diarization_note`) followed. Every item traces to an external evaluation of
0.2.4 (2026-07-27); each finding was reproduced against the code before
fixing. No new features.

### Fixed

- **`get_transcript` on a silent recording returns an honest empty payload
  instead of an error.** Silent recordings are a headline input, and the
  flagship `bug` prompt described the silent case as "the transcript is
  empty" while the tool raised `ToolError`. Now the payload has the same
  shape as a served transcript (`segments: []` / `text: ""` / `srt: ""`,
  zero counts), plus `transcript_available: false`, the stored `reason`, and
  a note routing to `search` (OCR is indexed) and `get_frames`/`get_moment`.
  `list_jobs` entries carry `has_transcript` — `segment_count: 0` alone
  could not tell "no audio stream" from "sound present, nobody spoke". The
  `bug` prompt text now matches the behavior it promises.
- **A diarization amend that changed nothing now says so.** A re-run with a
  different `num_speakers` can converge on the exact same clusters (measured
  on a real meeting: k=8 and k=9 both → 7 clusters, 0 of 605 segments
  relabelled — reported as plain success). The amend path now compares the
  roster and per-segment labels before/after and records
  `diarization.labels_changed`; when nothing changed, the summary and
  `get_transcript` serve one byte-identical note: "nothing was relabelled;
  num_speakers is a target the clusterer may not reach, not a constraint".
- **A no-op amend no longer silences the over-detection warning.** Any
  explicit `num_speakers` used to suppress the threshold-escalation note —
  so the exact flow the note recommends (ask the user, re-run with k) could
  end with the same dusty roster and no warning, reading as
  "human-confirmed". The note now survives when `labels_changed` is false.
- **Amend provenance is no longer ambiguous.** An amend re-saves the
  manifest but must not re-stamp `tool_versions` (that records what
  *transcribed* the job). The new `diarization.produced_by` records which
  version wrote the *current speaker labels*, on fresh runs and amends
  alike; after an amend the two can legitimately differ, and
  TROUBLESHOOTING explains the split.
- **`gc` now sweeps manifest-less partial directories.** A failure before
  the manifest exists could leave a directory holding only `job.lock`
  (litter 0.2.4 learned not to create but could not remove) — invisible to
  `list_jobs` and therefore to the age-based pass by construction. `gc`
  adds a second pass: manifest-less directories older than a day are
  removed under their own non-blocking job lock via the same
  "no manifest ⇒ safe" cleanup a live run uses; a held lock (a live run) is
  never touched. The CLI reports "removed N job(s)" and "swept M partial
  dir(s)" separately.
- **`job_lock` hardening.** The lock-retake loop (after a holder cleaned up
  the directory) now honors the same `wait_seconds` deadline as the flock
  wait, with a short backoff, instead of looping unbounded; a directory
  vanishing between `mkdir` and opening the lock file is retried instead of
  leaking a raw `FileNotFoundError`.
- Docs: two surviving falsified "in seconds" claims removed (README
  Speakers, TROUBLESHOOTING threshold advice), and the threshold-tuning
  advice corrected — a `TALKTHROUGH_DIARIZATION_THRESHOLD` change alone
  does not invalidate stored labels; applying it means a `force=true`
  re-run of the whole pipeline.

### Added

- The implausible-speaker-count note names the escape hatch for genuinely
  large meetings: "if that many people really did speak, pass
  num_speakers=N to confirm it" — the 16-cluster boundary itself is
  unchanged.
- Diarization model download failures that look like TLS interception
  (certificate errors) now point at `SSL_CERT_FILE` and TROUBLESHOOTING in
  the error text; CONTRIBUTING notes the same for first
  `pytest tests/integration` runs on corporate networks.
- `num_speakers` is documented as a target, not a guarantee — engine
  docstring, tool guidance, and README now agree, and the payload proves it
  via `labels_changed`.
- README Privacy names what the calling agent actually sees: only the
  payloads the MCP tools return (text and selected frames) in your existing
  session; talkthrough itself makes no LLM calls.

## [0.2.5] — 2026-07-31

An emergency one-line release: a dependency bound, no code or behavior
changes. Every step of the diagnosis below comes from an external incident
report — reproduced verbatim before fixing.

### Fixed

- **Server could not start in any freshly resolved environment since
  2026-07-28.** The MCP Python SDK released 2.0.0 on 2026-07-28, removing
  the `mcp.server.fastmcp` module this server imports; talkthrough-mcp
  declared `mcp>=1.28.1` with no upper bound, so every fresh resolve —
  `uvx` first installs, plugin installs, cache refreshes — picked the
  incompatible SDK and died at import with `ModuleNotFoundError: No module
  named 'mcp.server.fastmcp'` (Claude Code surfaces it as `Failed to
  reconnect … -32000`). Warm pre-2.0 uv caches kept working until their
  next re-resolve, which made the breakage look intermittent and
  machine-specific. The dependency is now bounded: `mcp>=1.28.1,<2`.
  Porting to SDK 2.x is a separate, unhurried task — the bound stays until
  it lands. Environments already broken heal on their next resolve; force
  it with `uvx --refresh "talkthrough-mcp[diarization]"`. The interim
  `--with 'mcp<2'` workaround is compatible and can be dropped.

## [0.2.4] — 2026-07-27

A growth-and-honesty micro-release: one new workflow prompt, a hygiene fix
in the failure path, two diarization honesty fixes sourced from a real
tester report, and a positioning refresh (README hero, new demo GIF, a
shareable benchmarks section). Fully additive: the `talkthrough-manifest/v1`
schema gains no fields, no new tools, no new dependencies.

### Added

- **`bug` workflow prompt** (`/talkthrough:bug` in Claude Code) — turn ONE
  screen recording into an evidence-backed GitHub issue draft: orient
  (transcript or search), pick the single highest-confidence bug, pull the
  `get_moment` evidence bundle, write a Heard/Saw/When/Expected checkpoint,
  then emit the draft (Title / Observed / Expected / Reproduction steps /
  Severity / Evidence with quote + `t_ms` + frame refs + OCR identifiers).
  Optional log correlation when logs are locally readable — exact lines
  quoted verbatim, deep correlation deferred to `correlate-with-logs`.
  Silent (narration-free) recordings — Game Bar's default — are a valid
  input: the prompt routes them through OCR search and frames. No issue is
  ever created online; the draft is the output. End-to-end example with a
  real silent recording: `examples/bug-from-silent-recording/`.
- **Implausible speaker-count warning.** Unconstrained (no `num_speakers`)
  clustering that detects more than 16 clusters now says so outright:
  "an implausible count: it likely over-split the speakers, and it is NOT
  a headcount" — same ask-the-user escalation, served byte-identical on
  the summary and `get_transcript` surfaces (a real large meeting
  "detected" 123 speakers; the old note undersold how wrong that number
  was).
- **`benchmarks/`** — the 150-run model battery repackaged as a shareable
  summary: score-by-task dot plot (light/dark), three findings, honest
  limits; linked from the README. Full matrices stay in
  `docs/MODEL-NOTES.md`.

### Fixed

- **A failure before the manifest exists no longer leaves a partial job
  directory behind** (tester report: a failed cold-start model download
  left a `job.lock`-only directory — invisible to `list_jobs` and
  harmless, but litter). The pre-manifest failure path now removes the
  directory; `job_lock` re-takes the lock on a fresh inode when the old
  holder cleaned up, so concurrent waiters keep their retry semantics.
  Completed jobs and amend targets (manifest present) are never touched.
- **"Fast amend" claims corrected everywhere.** The amend path re-runs
  ONLY diarization (whisper/frames/OCR are reused) — but the diarization
  stage itself re-scans the full audio, which takes minutes on long
  recordings (~12 min measured on a large meeting), not "seconds". The
  escalation note, `search`'s undiarized-job note, tool guidance, skill
  text, and TROUBLESHOOTING now say so honestly.

### Docs

- README hero: "Don't write a bug report. Record it." + the new demo GIF
  (30 s `/talkthrough:bug` storyline); neutral factual descriptions synced
  across `pyproject.toml`, `server.json`, and both plugin manifests.

## [0.2.3] — 2026-07-18

Fail-fast and honesty-contour fixes, sourced from the same-day external
evaluation of 0.2.2 and from holes that release itself introduced. Fully
additive patch: the `talkthrough-manifest/v1` schema gains no fields, no
new tools, no new dependencies — every new field lives in server responses
only, so existing processed jobs serve the new data with no migration.

### Fixed

- **A failed explicit re-diarize no longer erases stored labels.** With the
  0.2.2 embedding-model gate, a mistyped `TALKTHROUGH_DIARIZATION_EMB_MODEL`
  plus `diarize=true` on a job with WORKING labels reached the amend path,
  failed to build the engine, and overwrote good labels with
  `available: false` (the 0.2.2 evaluation's one design caveat). The amend
  now constructs the diarizer BEFORE the WAV extract and before any store
  write: construction failures (bad model env, dead model download) raise a
  clean tool error and the stored job stays byte-identical. The boundary,
  documented in TROUBLESHOOTING: a failure *inside* diarization after
  successful construction still degrades to `available: false` with the
  reason, and a fresh (non-amend) run keeps degrading as before — there are
  no labels to lose there.

### Added

- **The threshold-escalation note now reaches transcript-first agents.**
  The ask-the-user note (over-detected threshold roster) lived only in the
  `process_media` summary — an agent starting from `list_jobs` →
  `get_transcript` never saw it (a 0.2.2 evaluation run mis-mapped a
  speaker exactly that way). `get_transcript` headers now carry the same
  byte-identical text as `diarization_note` next to the roster; absent on
  jobs with an explicit `num_speakers` or a clean roster.
- **`list_jobs` stops implying a headcount.** A diarized entry's
  `"speakers"` field serves the raw detected count — on threshold-mode
  over-detection that read as "28 people attended". Such entries now carry
  `"speakers_with_30s_plus"` alongside; `"speakers"` itself is unchanged
  for compatibility.
- **Zero-hit searches explain themselves** (payload honesty, both notes new):
  - `speaker=` with a label outside a diarized job's roster returns
    `hits: []` plus a note naming the label and the valid range
    (`label 'S99' is not in this job's roster (S1-S7)`) — an empty result
    stops being indistinguishable from "that voice never said it".
  - A multi-word query with zero hits gets a note explaining per-segment
    word-AND matching; when the words DO meet across two adjacent segments
    (the "recurring invites" class from the 0.2.2 evaluation), the note
    names the spot: `the words appear together around t_ms=X … read
    get_transcript there`. A cheap adjacent-pair scan, transcript only —
    the hit contract, single-word behavior, and non-empty payloads are
    byte-identical to 0.2.2.
- **`longest_turn_ms` in every roster entry** (summary and `get_transcript`,
  computed at serve time): the start of that speaker's longest turn — the
  exact instant to pull frames for name plates / the active-speaker
  highlight when mapping labels to people
  (`get_frames(at_ms=<longest_turn_ms>)`).

### Docs

- Guidance pack, one regen: minutes/spec prompts now order "copy `t_wall`
  VERBATIM from the payload — never compute it" (a 0.2.2 evaluation run
  hand-derived one and slipped an hour); the meeting-actions screen check
  is MANDATORY on video jobs and anchored at each label's
  `longest_turn_ms` (uptake of the optional wording was probabilistic);
  meeting-actions and triage-recording carry the homophone rule ("profit"
  vs on-screen "Prophet" — trust OCR/frames for name spellings); the MCP
  server `instructions` string gains one canon-keys sentence — an
  experiment aimed at clients that read neither tool descriptions nor MCP
  prompts, measured by the release battery.
- TROUBLESHOOTING: "updated the plugin but the server is old" (running
  sessions keep the MCP process until restart); the reprocess-cost rule
  (explicit model change = full re-run, up to half the recording's duration
  on a laptop — measured 65 min → 28.5 min); the fail-fast boundary above.

## [0.2.2] — 2026-07-18

Search ergonomics and honesty fixes, each sourced from the v0.2.1 release
battery or the external evaluation of 0.2.1 on a real corporate meeting.
Fully additive patch: the `talkthrough-manifest/v1` schema gains no fields
at all, no new tools, no new dependencies — every new field below lives in
server responses only, so existing processed jobs serve the new data with
no migration.

### Added

- **Word-level search** (#16) — a multi-word `search` query now hits when
  EVERY whitespace-separated word matches as a substring, in any order at
  any distance; both sides are normalized with casefold + ё→е + NFC. A
  single-word query behaves exactly as before, and the stem trick from the
  guidance now closes Russian case endings: «кнопк отправк» finds both
  «Кнопка отправки» and «кнопку отправки» (no stemming — deliberately).
  Hit payloads are unchanged. Verified on a real RU screencast: «карточк
  справ» lands on «…увидеть карточку справа…», «заявк» reaches both the
  spoken phrase and the on-screen bot reply via OCR.
- **`search(…, speaker="S2")`** — filter transcript hits to one diarized
  voice (label case-insensitive). OCR hits are excluded when the filter is
  active — on-screen text has no voice — and the payload says so. On an
  undiarized job the response is honestly empty with a note naming the fix
  (`diarize=true`, fast amend) instead of an error. `query` stays required:
  "everything S2 said" is `get_transcript`'s job.
- **`media_kind` in `get_transcript`** — `"video"` or `"audio"` next to
  `language`, so minutes writers can't mislabel a video job "audio-only"
  (an Opus slip observed by the external evaluation) —
  payload-over-description, again.
- **Vocabulary-echo trim** — whisper replays `initial_prompt` (the
  `vocabulary`) over quiet opening seconds; on a real 73-minute meeting
  the echo swallowed the actual first words. Segments inside the first
  ~90 s that are ≥80% vocabulary tokens AND (a token repeated 3+ times OR
  a near-verbatim vocabulary prefix) are dropped, logged, and counted in
  the summary as `transcript.vocabulary_echo_trimmed` (present only when
  > 0). A live roll-call («на встрече присутствуют Анастасия, Диана и
  Влад») has connecting words, fails the 80% bar, and survives — guarded
  by a dedicated unit test.

### Changed

- **Threshold-mode over-detection now escalates to the user.** The 0.2.1
  note called `speakers_with_30s_plus` "the likely headcount" — the
  external evaluation falsified that (it said 4 on a true-2 meeting), so
  the server no longer guesses headcounts at all. The note now instructs
  the agent to ASK THE USER how many people spoke (the talk-time roster
  right above is the material for that question) and to re-run with
  `num_speakers=N` — the amend takes seconds, whisper is not re-run.
  `speakers_with_30s_plus` stays in the payload as one signal among
  several, without the claim.
- **Explicit `diarize=true` re-diarizes when the embedding model changed.**
  A job diarized under one `TALKTHROUGH_DIARIZATION_EMB_MODEL` used to
  serve its old labels forever; now an explicit request on a job whose
  stored `diarization.embedding_model` differs from the currently resolved
  one re-runs just the diarization stage (whisper untouched) — the mirror
  of the explicit-whisper-model reuse rule. An env change without explicit
  intent still never invalidates the store.

### Fixed

- **`diarization_amended` reflects the outcome.** A failed amend (e.g. a
  model download dying on corporate TLS) used to return top-level
  `diarization_amended: true` right above `diarization.available: false`.
  The flag is now set only when the amend actually landed labels; failures
  keep the transcript and report `available: false` with the reason.

### Docs

- Tool guidance: search examples teach word-AND semantics and the
  `speaker=` filter; `process_media` gains the meeting recipe
  (`large-v3-turbo` + attendee `vocabulary` + `num_speakers` — turbo's
  extra cost is trivial next to frames+OCR) and the ask-the-user line for
  noisy threshold rosters.
- `meeting-actions` prompt (and the Agent Skill): speaker-mapping evidence
  now includes on-screen sources — meeting-app name plates, the
  recording's title card, the active-speaker highlight — the exact
  evidence a freeform evaluation run used to name speakers while the
  command-constrained run left them "unidentified".
- `triage-recording` prompt: the findings keys are declared EXACTLY
  (`quote`, `frame_refs`, …, no `quotes[]`/`evidence[]` wrappers) — aimed
  at the one runner that kept drifting from the canon.
- TROUBLESHOOTING: a "Corporate networks" section (`HF_HUB_DISABLE_XET=1`
  for stalled whisper downloads, `SSL_CERT_FILE` for TLS-inspected
  diarization downloads). MODEL-NOTES: EN homophone names resist
  `vocabulary` at every config ("prophet" → "profit") — the multi-modal
  OCR redundancy is the compensation.
- Windows wording: the CI job is no longer labeled "best-effort" —
  promotion to a required branch check is planned right after this
  release's green week.

## [0.2.1] — 2026-07-18

Quality quick-wins, each grown out of a concrete v0.2.0 release-battery
failure (see `docs/MODEL-NOTES.md`). Fully additive: the manifest schema
and the model defaults are untouched, and every field below is computed at
serve time — existing processed jobs gain the new data with no migration.

### Added

- **Per-frame validity spans** (#14) — every frame served by `get_frames` /
  `get_moment` carries `valid_from_ms` / `valid_to_ms`: the interval during
  which the screen looked like that keyframe (duplicates prove continuity,
  so a span runs to the next unique keyframe). "Was X on screen at t?"
  becomes a data lookup instead of a `duplicate_of`-chain inference. Honesty
  at the edges: when frame extraction hit its cap (`cap_hit`), the last span
  ends at the last extracted sample plus one sampling step — never at media
  end. `extract_frame` responses stay span-free (an exact instant by
  definition); the `get_moment` "no unique keyframe inside the range" note
  remains as the secondary, prose explanation. Verified on a real 73-minute
  meeting job processed by v0.2.0: spans appear with no reprocessing and
  cover the requested moments inside deduplicated static stretches.
- **OCR script pack auto-selected from the speech language.** The v0.2.0
  battery found Cyrillic UI text unreadable by the default Latin+Chinese
  recognition models — on a real RU bug screencast, the bot's on-screen
  reply «Я готовлю вашу заявку…» was invisible to `search`. Transcription
  runs before OCR, so when `TALKTHROUGH_OCR_LANG` is not set and the
  detected narration language maps to a script pack (`ru`→`eslav`,
  `ja`→`japan`, `ko`, `ar`, `hi`, …), that pack now becomes the derived
  default. The explicit env always wins; Latin-script languages (es/fr/de/
  en) never switch packs; pack models remain a one-time download. Proven on
  two scripts: the same real RU screencast (the bot reply is now found by
  `search`, 8 OCR hits, Latin UI text still read) and a new committed
  Japanese fixture whose katakana heading the default model cannot read.
- **Frame-sampling honesty note.** On long recordings the adaptive keyframe
  floor means sampling every ~N seconds, not every second; the
  `process_media` summary now says so (`frames.sampling_interval_s` + a
  note pointing at `extract_frame`) whenever the floor exceeds 1 s — the
  same payload-over-description principle that fixed threshold-mode
  headcounts in 0.2.0.

### Docs

- Tool guidance teaches the new data: check that a frame's span covers the
  moment you cite; `cap_hit`/`sampling_interval_s` in a summary is the cue
  to raise `TALKTHROUGH_MAX_FRAMES` or use `extract_frame` for slide hunts.
- attendees → `vocabulary` recipe: names the transcriber has seen in
  `initial_prompt` survive STT instead of degrading into look-alike words
  («Анастасия» → "in a station" class), so `process_media` examples and the
  `meeting-actions` prompt now say to pass attendee names in `vocabulary`.

### CI

- The Windows job installs the `[diarization]` extra and runs a diarize
  smoke over the two-voice fixture with a JSON assert on the speaker roster
  (engine failures degrade by design, so exit codes alone prove nothing).
  Its first run immediately caught a real Windows quirk — redirected stdout
  falling back to cp1252 — now fixed with `PYTHONUTF8`. Windows remains
  best-effort.

## [0.2.0] — 2026-07-16

Speaker diarization (#4) and absolute frame paths (#13). Additive minor:
every 0.1.x call keeps working unchanged, non-diarized runs pay nothing for
the new machinery (the engine is never even imported), and manifests only
gain fields when diarization actually ran.

### Added

- **Speaker diarization** (#4) — opt-in, fully local, via the new
  `[diarization]` extra (`uvx "talkthrough-mcp[diarization]"`,
  sherpa-onnx ≥ 1.13.4, no torch/accounts/GPU):
  - `process_media(diarize=true, num_speakers=N?)` and CLI
    `process --diarize [--num-speakers N]`. Speakers are labeled `S1`/`S2`/…
    by first appearance; every transcript segment gets a `speaker` by
    dominant time-overlap against the diarized turns (whisperX-style,
    segment-level).
  - Surfaced everywhere: roster (talk time, turn count) in the
    `process_media` summary and the `get_transcript` header; `speaker` on
    segments and `search` hits; `S1:` prefixes in the `text` (at speaker
    changes) and `srt` (every cue) formats; `speakers_in_range` in
    `get_moment`; a `speakers` count in `list_jobs`. New fields appear only
    on diarized jobs.
  - **Amend path:** `diarize=true` (or a differing explicit `num_speakers`)
    on an already-processed job re-runs only diarization — whisper is not
    re-run, labels land in the stored manifest in seconds.
  - Degradation matrix: explicit `diarize=true` without the extra fails fast
    with the install hint BEFORE transcription starts; `TALKTHROUGH_DIARIZE=on`
    without the extra warns and degrades; any engine/runtime failure records
    `diarization.available=false` + reason and keeps the transcript.
  - Models (pyannote segmentation-3.0 ONNX, MIT + NeMo TitaNet-Small,
    Apache-2.0, by default — the accept-eval winner on a real 3-speaker
    meeting and RU/EN/ES clips; WeSpeaker ResNet34-LM and 3D-Speaker
    CAM++ remain selectable) download once from pinned k2-fsa release URLs
    with sha256 verification into `<TALKTHROUGH_HOME>/models/diarization/`;
    warm runs stay zero-network (verified with blocked sockets). Measured on
    an M-series CPU (4 threads): a 26-minute meeting diarizes in ~2 minutes
    (RTF ≈ 0.08). Env knobs: `TALKTHROUGH_DIARIZE`,
    `TALKTHROUGH_DIARIZATION_THRESHOLD`, `_SEG_MODEL`/`_EMB_MODEL`
    (allowlist name or local `.onnx` path = offline preseed), `_THREADS`.
  - `meeting-actions` prompt now maps `S*` labels onto attendees via
    self-introductions/vocatives and puts owners on action items.
  - Manifest schema stays `talkthrough-manifest/v1` (additive): `speaker` on
    segments + a `transcript.diarization` block with compact
    `[t0_ms, t1_ms, "S1"]` turn triplets (kept for range queries and future
    word-level splitting). Manifests without diarization serialize
    byte-identically to 0.1.x output (modulo the now-correct version stamp
    in `tool_versions` — see Fixed below); verified against a real v0.1.3
    checkout on the same recordings.
- Generated install configs are batteries-included: every one-click button,
  per-engine snippet, the Claude Code plugin, and the Claude Desktop bundle
  now launch `uvx "talkthrough-mcp[diarization]"`, so "who said what" works
  without a reinstall for users who never read the docs. The PyPI package
  itself is unchanged — `uvx talkthrough-mcp` (and the MCP registry entry)
  remain the lean, diarization-free install, and diarization still runs
  only when a call asks for it.
- `extract_frame` returns the absolute `path` of the extracted file, and
  `get_frames`/`get_moment` name each served frame's absolute `path` (#13) —
  "save this screenshot elsewhere" becomes the calling agent's own file copy,
  and the server's write boundary stays `TALKTHROUGH_HOME` (no `output_path`
  parameter by design).

### Fixed

- `Manifest.from_dict` now ignores unknown dataclass keys, so manifests
  written by newer package versions load instead of raising `TypeError`.
  The inverse still holds: 0.1.x cannot read manifests that already contain
  diarization fields — noted here as the downgrade boundary.
- `tool_versions["talkthrough-mcp"]` in manifests recorded a stale hardcoded
  `0.1.0` on every release; `__version__` now derives from the installed
  package metadata.
- Tool guidance now teaches two rules the agent-battery test pass proved
  necessary: any multi-person recording gets `diarize=true` as part of normal
  analysis (a naive "summarize this meeting" prompt previously left speakers
  off in 9 of 12 model runs), and threshold-mode cluster counts are voices,
  not people. Threshold-mode responses also carry `speakers_with_30s_plus`
  plus a one-line note pointing at `num_speakers`.
- Diarization rosters in tool responses are capped at the top 12 speakers by
  talk time (`speakers_truncated` reports the rest; the manifest keeps all) —
  a real 43-minute workshop in threshold mode produced 123 clusters, which
  would have flooded every `get_transcript` response.
- `TALKTHROUGH_DIARIZATION_THRESHOLD` now rejects non-positive values with a
  warning instead of passing them into the native clustering.
- Long recordings no longer lose their tail frames: the fixed 1 s keyframe
  selection floor meant the 600-frame budget covered only the first ~10
  minutes of a meeting (a 73-minute real meeting surfaced it — slides shown
  after minute 10 were invisible to `get_frames`/OCR search). The floor now
  adapts to `max(1 s, duration / max_frames)`, spreading the same budget
  across the entire recording; scene changes still capture at any instant.
  Videos short enough for the budget at 1 fps are extracted byte-identically
  to before.

## [0.1.3] — 2026-07-12

Hardening from a hostile-input test pass (silent recordings, odd containers,
corrupt files, offline machines) ahead of the public announcement.

### Fixed

- **Warm runs are now zero-network.** faster-whisper loads the model from the
  local cache first (`local_files_only=True`, one-time download only on a
  cache miss); previously huggingface_hub revalidated repo metadata against
  huggingface.co on every model load — even fully cached — contradicting the
  "no runtime network beyond one-time downloads" promise. Verified by running
  the full pipeline with all sockets blocked.
- Tool-failure messages name the binary (`ffprobe failed: …`) instead of
  leaking the full venv path; the static-ffmpeg fallback log no longer claims
  a download on every run.

### Docs

- Quickstart names its one real prerequisite (uv) with install one-liners.

Also verified in this pass (no changes needed): videos without an audio
track process gracefully (`transcript.reason: "no audio stream"`, frames+OCR
still work), `.webm`/`.mkv`/2-second/unicode-name inputs work, corrupt files
fail cleanly with exit code 2, and Intel-mac installs resolve (onnxruntime
1.23.2 ships x86_64 wheels).

## [0.1.2] — 2026-07-11

### Fixed

- All 7 tools now ship MCP `ToolAnnotations` (readOnly/destructive/idempotent/
  openWorld hints). Non-interactive clients gate approvals on these — OpenAI
  Codex `exec` silently cancelled every un-annotated call ("user cancelled
  MCP tool call"); with hints, Codex drives talkthrough headlessly. Hints are
  honest: only `process_media`/`extract_frame` write, and only inside
  `TALKTHROUGH_HOME`.

## [0.1.1] — 2026-07-11

Launch-day fixes from a full as-a-new-user E2E pass (every install path,
a real 2-minute narrated recording, contract-validated triage).

### Fixed

- An explicit per-call `model=` (tool param / CLI `--model`) that differs from
  the stored transcript's model now reprocesses the file instead of silently
  returning the old model's transcript. Changing the env default still keeps
  the store intact; `force=true` behaves as before.

### Docs

- Removed the dead Goose one-click button (their deep-link endpoint 404s
  ecosystem-wide); the Goose config stays in the client matrix.
- Troubleshooting: `pip install` on Python < 3.11 prints a confusing
  "No matching distribution" — explained, with the `uvx` escape hatch.

## [0.1.0] — 2026-07-11

First public release.

### Added

- Deterministic local pipeline: ffprobe validation → wall-clock resolution →
  faster-whisper STT (timestamped segments) → one-pass scene keyframes
  (scaled ≤1568 px) → dHash dedup → RapidOCR of unique frames →
  `talkthrough-manifest/v1`.
- Content-addressed job store (`~/.talkthrough/jobs/<sha256[:16]>`):
  idempotent `process_media`, instant re-calls, `gc`.
- Wall-clock ladder: `recorded_at` override → QuickTime creationdate tag →
  container `creation_time` → mtime−duration; `t_wall` on every timestamped
  output.
- 7 MCP tools (`process_media`, `get_transcript`, `get_frames`, `get_moment`,
  `search`, `extract_frame`, `list_jobs`) with 10-15 usage examples embedded
  in every description (unit-gated); `get_moment`/`search` pick a
  window-representative frame, not the nearest cross-scene keyframe.
- 5 server prompts (`triage-recording`, `spec-from-workshop`,
  `backlog-from-demo`, `meeting-actions`, `correlate-with-logs`), mirrored in
  `examples/prompts/` with a no-drift test.
- Multilingual support: language auto-detection with `language_probability`
  in manifest and summary; per-call `model=` parameter validated against the
  faster-whisper alias list (incl. `large-v3-turbo`); prompts mandate digests
  in the narrator's language with verbatim quotes; on-screen-text OCR script
  packs via `TALKTHROUGH_OCR_LANG` / `TALKTHROUGH_OCR_PARAMS`.
- CLI: `serve` (stdio MCP, default) / `process [--json] [--model]` / `gc`.
- ffmpeg resolution ladder with pip-only `static-ffmpeg` fallback; OCR
  gracefully disables (`TALKTHROUGH_OCR=off` or import failure).
- Engine-agnostic integrations: `integrations/<engine>/` for 12 MCP clients,
  a full Claude Code plugin, the cross-engine agent skill
  (`.agents/skills/talkthrough/`), `AGENTS.md`, `llms-install.md`, `llms.txt`,
  and the MCP-registry `server.json` — every artifact generated from one
  source of truth (`scripts/gen_integrations.py`) and byte-pinned by tests,
  including the README install matrix.
- Examples: genericized triage agent, findings-contract JSON Schema,
  composition patterns, GitHub-issues recipe.
- Docs: FAQ + Limitations in README, `docs/TROUBLESHOOTING.md`,
  `docs/DESIGN.md`; PEP 561 `py.typed` marker.
- CI: ubuntu (lint, mypy strict, unit + integration + e2e over committed
  synthetic EN/RU fixtures) + macos (lint, unit) + windows best-effort smoke;
  actions pinned by commit SHA; release workflow via PyPI Trusted Publishing
  (inert until the first `v*` tag).
