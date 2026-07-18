# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

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
