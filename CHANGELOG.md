# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

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
