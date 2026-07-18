# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

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
