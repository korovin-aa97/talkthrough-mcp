# Design

## Shape

A deterministic media pipeline (no LLM) behind an MCP server with lazy
retrieval tools. Processing happens once per file; everything an agent asks
for afterwards is served from the on-disk manifest without re-reading the
source — except `extract_frame`, which deliberately re-decodes the source for
exact instants.

```
             process_media(path)
                    │
   ┌────────────────▼─────────────────────────────────────────┐
   │ 1 sha256(file) → job_id      (idempotence: hit → return;  │
   │     explicit diarize on a stored job → amend: stage 4b    │
   │     only, whisper untouched)                              │
   │ 2 ffprobe: streams, duration, tags   → caps + disk check  │
   │ 3 wall-clock resolver (override > qt tag > tag > mtime)   │
   │ 4 ffmpeg → 16 kHz mono WAV → faster-whisper segments      │
   │ 4b (opt-in) sherpa-onnx diarization on the same WAV       │
   │     → S1/S2 turns → segment attribution by max overlap    │
   │ 5 ffmpeg ONE pass: select(scene>0.10 ∨ Δt≥floor);         │
   │     floor = max(1s, duration/max_frames) — the frame      │
   │     budget covers the WHOLE recording, head never wins    │
   │     + scale ≤1568px + showinfo pts → t<ms>.jpg            │
   │ 6 dHash dedup (consecutive, Hamming ≤4 → duplicate_of)    │
   │ 7 RapidOCR over unique frames → newline-separated box text│
   │ 8 manifest.json                                           │
   └────────────────┬─────────────────────────────────────────┘
                    ▼
   get_transcript / get_frames / get_moment / search / list_jobs
                    (manifest-only, paginated, capped)
   extract_frame ──────────────── re-decodes the SOURCE file
```

## Module map (`src/talkthrough_mcp/`)

| Module | Responsibility |
|---|---|
| `server.py` | MCPServer app: 9 tools (8 local + `process_url`), 6 prompts, progress, MCP error mapping |
| `guidance.py` | Tool descriptions (10-15 examples each) + prompt templates; unit-gated |
| `cli.py` | `serve` (default) / `process` / `process-url` / `gc` |
| `core/pipeline.py` | Orchestrates stages, caps, progress callbacks, summary |
| `core/url_ingest.py` | The network boundary's control plane: URL classification, redaction, the destination gate (public addresses only), the URL index, managed-source install, `process_url` orchestration |
| `core/url_download.py` | The two downloaders: direct HTTPS via httpx pinned to the checked address; YouTube via `yt-dlp` with an allowlisted option set (`[url]` extra) |
| `core/ffmpeg.py` | Binary ladder: system ffmpeg → `static-ffmpeg` auto-download |
| `core/probe.py` | ffprobe → `MediaInfo` (streams, duration, container tags) |
| `core/wallclock.py` | The resolver ladder + `t_wall` rendering |
| `core/audio.py` / `core/stt.py` | WAV extraction; faster-whisper (CPU int8, VAD) |
| `core/diarize.py` | Speaker diarization: sherpa-onnx engine behind the `[diarization]` extra (pinned-URL+sha256 model cache, zero-network warm loads) + pure attribution math (S1/S2 by first appearance, maximum-overlap word assignment with segment fallback, roster). Vendors a second ONNX Runtime (~30 MB RSS) — accepted trade-off vs. sharing rapidocr's |
| `core/frames.py` | One-pass keyframe extraction + showinfo parsing + exact re-extract |
| `core/dedup.py` | Pillow-only dHash (9×8) + Hamming marking |
| `core/ocr.py` | RapidOCR wrapper; `TALKTHROUGH_OCR=off` or import failure → graceful off |
| `core/manifest.py` | Schema, save/load, SRT, slicing, frame queries, search |
| `core/jobs.py` | Content-addressed store, per-job thread lock + POSIX flock, staged rebuild commit + interrupted-commit recovery, damaged-manifest quarantine, listing, gc |
| `core/errors.py` | `ValidationError` / `UnknownJobError` / `AudioOnlyJobError` / `ToolFailureError` |

## Job store

```
~/.talkthrough/                  (TALKTHROUGH_HOME overrides)
├── jobs/<sha256(file)[:16]>/
│   ├── manifest.json
│   ├── manifest.json.damaged-<ts>   (an unreadable manifest a rebuild set aside)
│   ├── frames/t<ms 8-digit>.jpg
│   ├── source/<youtube-<id>|direct-<hash12>>.<ext>   (URL jobs: the kept download)
│   ├── extracts/…               (extract_frame outputs)
│   ├── .reprocess-<id>/         (hidden rebuild workspace, gone after commit)
│   └── job.lock
├── urls/<sha256(key)[:32]>.json (URL index: key → job_id; no raw URL)
└── downloads/.dl-<random>/      (private download staging, gone after install)
```

Content addressing makes renames/moves free and `process_media` idempotent:
the second call on the same bytes returns the stored summary in milliseconds.
`force=true` rebuilds (e.g. to re-anchor `recorded_at` or change vocabulary).

A rebuild of an existing job is staged inside the job directory and
published by three renames under the job lock: `frames/` → workspace
`previous-frames/`, staged `frames/` → live, staged `manifest.json` → live.
Catchable failures roll the frames back before the manifest moves. A hard
kill between the renames is repaired on the next lock acquisition
(`process_media`, `label_speakers`, `gc`): a workspace holding
`previous-frames/` proves the sequence started, so it is either finished
(`rolled_forward`) or reversed (`rolled_back`) and the response reports it
in `recovery_note`; only workspaces whose commit never began fall under the
age-based gc sweep. Read tools never take the lock; they compare the frame
index with the directory listing and report `missing_frame_count` plus an
`integrity_note` instead of silently serving fewer frames. An unreadable
`manifest.json` is never a cache hit and never a bare decoder error: the
rebuild keeps it as `manifest.json.damaged-<ts>` and says so in
`manifest_recovery_note`. The disk preflight of a rebuild reserves the
current `frames/` size on top of twice the media size, because the previous
keyframes stay on disk until the commit.

## Network boundary (`process_url`, 0.4.0)

Everything above is network-free after one-time model downloads. The one
exception is deliberate and isolated in two modules:

```
process_url(url)
  → classify (one YouTube video | any other https:// URL; playlists,
    channels, credentials, non-443 ports, http://, file:// refused)
  → a non-YouTube URL is tried as a media file first (our gated, pinned
    downloader); a page or an HTTP error hands it to the page reader
    (yt-dlp: ~1800 site extractors + generic HTML5/HLS player detection,
    no cookies); known page hosts skip the media attempt
  → URL index hit and refresh=false → the stored job, zero network
  → destination gate: every DNS answer must be a public address
  → download into ~/.talkthrough/downloads/.dl-*/ under caps:
      bytes (TALKTHROUGH_MAX_DOWNLOAD_BYTES), duration (provider metadata,
      then ffprobe), free disk, redirects (≤5, each hop re-validated),
      wall time — direct: httpx pinned to the checked address (SNI + Host
      carry the name); YouTube: yt-dlp with an allowlisted option set
  → ffprobe verification → sha256 → jobs/<id>/source/<talkthrough name>
  → the ordinary pipeline (mtime wall-clock rung disabled: a download time
    is not a recording time; the provider's publication time is stored
    apart from wall_clock)
  → URL index entry (hashed key → job_id) → the ordinary retrieval tools
```

Design rules: job ids stay content hashes (two URLs with the same bytes, or
a local file processed earlier, converge on one job — the manifest gains
`media.origin` and `media.managed_source` additively); a page video is
also indexed by provider identity (`site:<extractor>:<id>`), so two URL
forms of one Instagram/TikTok video converge before a second download; the
raw URL, its query and userinfo never reach a manifest, the index, a log
line, a progress message or an error (`url_ingest.redact` is the single
choke point and a canary test pins it); `extract_frame` decodes the kept
source, so URL jobs never need the network again; `gc` deletes the source
with its job and drops index entries whose job is gone. Same-URL calls
serialize on a URL lock, and the install + pipeline run under the job lock,
so a second caller finds the mapping instead of downloading twice and a
failing call can never remove a source another call just installed.
Residual, documented in SECURITY: the page reader follows embedded players
and redirects with yt-dlp's own client; only the named host is gated.

## Manifest schema (`talkthrough-manifest/v1`)

```jsonc
{
  "schema": "talkthrough-manifest/v1",
  "job_id": "4d0695c8ab1e38ac",
  "created_at": "2026-07-10T19:32:11+00:00",
  "media": { "path", "filename", "kind": "video|audio", "duration_s",
             "size_bytes", "width", "height", "video_codec",
             "has_audio", "has_video",
             "origin"?: { "kind": "youtube|direct_url", "provider",
                          "url_sha256", "provider_id"?, "host"?, "title"?,
                          "published_at"?, "downloader"?,
                          "downloaded_bytes"?, "downloaded_at"? },
             "managed_source"?: "source/youtube-<id>.mp4" },
  "wall_clock": { "start_utc", "tz_offset_min", "source", "confidence" } | null,
  "transcript": { "available", "reason", "language", "model",
                  "segments": [{ "seq", "t0_ms", "t1_ms", "text", "speaker"?,
                                  "source_seq"? }],
                  "words"?: [[t0_ms, t1_ms, " raw token"], …],
                  "diarization"?: { "available", "reason", "engine",
                                    "engine_version", "segmentation_model",
                                    "embedding_model", "requested_num_speakers",
                                    "detected_num_speakers", "threshold",
                                    "speakers": [{ "label", "talk_time_ms",
                                                   "turn_count", "first_ms",
                                                   "last_ms" }],
                                    "speaker_names"?: { "S1": "Alice" },
                                    "speaker_name_evidence"?: { "S1": "frame proof" },
                                    "speaker_names_pending_review"?: { "S1": "Alice" },
                                    "speaker_name_evidence_pending_review"?: {
                                      "S1": "frame proof"
                                    },
                                    "speaker_names_pending_review_context"?: {
                                      "S1": {
                                        "source_detected_num_speakers": 2,
                                        "source_requested_num_speakers": 2,
                                        "source_produced_by": "0.3.2",
                                        "talk_time_ms": 18342,
                                        "turn_count": 4,
                                        "longest_turn_at_ms": 5210,
                                        "longest_turn_duration_ms": 7040
                                      }
                                    },
                                    "turns": [[t0_ms, t1_ms, "S1"], …] } },
  "frames": { "count", "unique_count", "cap_hit",
              "items": [{ "ms", "file", "duplicate_of"?, "ocr_text"? }] },
  "caps": { "max_seconds", "max_frames", "scene_threshold", "ocr" },
  "tool_versions": { "talkthrough-mcp", "ffmpeg", "faster-whisper", "rapidocr" }
}
```

`source_seq` is an internal, manifest-only origin marker on word-split
segments. It lets a later diarization amend regroup the words from the same
original Whisper segment; MCP responses do not expose it or the raw `words`
array.

## Wall-clock ladder

| Rung | Source | Confidence | tz offset |
|---|---|---|---|
| 1 | `recorded_at` param | `exact` | from the ISO string (naive → machine-local) |
| 2 | `com.apple.quicktime.creationdate` | `high` | carried by the tag (QuickTime Player; pre-macOS-26 ⌘⇧5) |
| 3 | container `creation_time` | `medium` | unknown (UTC instant only); macOS 26+ ⌘⇧5/ReplayKit lands here |
| 4 | file mtime − duration | `low` | machine-local |
| 5 | — | wall_clock = null | — |

`t_wall` renders in the recording-local offset when known (log correlation
reads naturally), else UTC. Rung 4 subtracts the duration because screen
recorders finalize the file when recording STOPS. Tag values with year <1972
are treated as encoder garbage and skipped.

## Token-budget rules

The whole tool surface is built to keep responses small:

- `process_media` returns a summary only: counts + a ~15-segment preview.
- `get_transcript` hard-caps at ~8k tokens (~30k chars) and returns
  `truncated` + `next_start_ms` for continuation.
- `get_frames` serves unique frames by default, max 6 images per call,
  keyframes pre-scaled to ≤1568 px wide (vision-model sweet spot) at
  extraction time — normal serving never re-reads the video.
- `get_moment` bundles ≤3 frames + the transcript slice for one remark.
- `search` returns pointers (`t_ms`/`t_wall`/nearest frame), not payloads,
  capped at 50 hits. A zero-hit word-AND may add one ≤240-character,
  query-aware adjacent-segment quote; it samples both sides of the boundary
  and explicitly says when not every matched token fits.
- Diarized rosters expose at most three OCR `name_candidates` per speaker,
  each ≤80 characters. The deterministic filter rejects obvious UI chrome,
  digits, URLs/paths, and long copy; candidates remain unverified hints and
  never become active names without `label_speakers`.
- Pending speaker identities are capped on response surfaces and stay
  separate from active names. Each served entry may carry a bounded anchor
  to its source roster; stale labels can only be removed with an explicit
  null patch and never become active automatically. Entries beyond the cap
  are still removable, so their labels (not names) are listed in
  `speaker_names_pending_review_hidden_labels` (itself capped at 100).
  The response-only `speaker_names_pending_review_dropped` report exists
  for manifests where an active and a pending name share one label; the
  tool surface itself cannot create that state (`label_speakers` reviews a
  label out of pending review, and a relabelling amend leaves no active
  names behind), so it fires only for hand-edited or foreign manifests.
- Tool descriptions themselves are budgeted: one-line examples, ≤120 chars
  each (gated by `tests/unit/test_guidance.py`).

## Guidance layer

Models use tools far better when the server ships usage guidance:

1. Every tool description embeds 10-15 one-line examples: canonical calls,
   param combos, agent intents mapped to the right call, edge cases
   (audio-only, `wall_clock=null`, truncation continuation), and
   anti-examples redirecting to the better tool.
2. Six `@mcp.prompt()` workflow prompts mirror `examples/prompts/*.md` and
   the Claude Code plugin commands — everything renders from the same
   templates in `guidance.py` via `scripts/gen_integrations.py`, which also
   emits every `integrations/<engine>/` adapter; a unit test byte-pins all
   generated files so they cannot drift.

## Testing strategy

- **unit** — pure logic, no ffmpeg/model downloads: pts regex, wall-clock
  ladder (incl. captured ffprobe JSON), dHash pairs, manifest round-trip,
  SRT, job hashing/gc, the guidance quality gate, diarization attribution
  math + model-cache resolver (faked downloads) + the diarize request
  matrix; URL ingestion without a network — the classification matrix, the
  destination gate against a fake resolver, redirect/cap/redaction cases
  over an `httpx.MockTransport`, a stub `yt_dlp` module, and the whole
  `process_url` flow (index reuse, refresh, content convergence, concurrent
  calls, failure cleanup) with a stub downloader on the real pipeline.
- **integration** — real ffmpeg + whisper `tiny` + RapidOCR over committed
  synthetic fixtures (3-scene screencast with known `creation_time`;
  audio-only meeting; two-voice meeting for diarization — always with an
  explicit `num_speakers` in CI, threshold mode is the flaky path): keyword
  survival, scene-boundary frames, wall-clock math, OCR content, dual-source
  search, caps, idempotence, force re-anchor, diarization attribution
  against fixture facts + the whisper-untouched amend path.
- **e2e** — a real MCP stdio client session against `uv run talkthrough-mcp`:
  discovery (schemas + examples on the wire), prompts, processing, image
  content blocks, search with `t_wall`, SRT.

Fixtures are generated once on macOS by `tests/fixtures/make_fixtures.py`
(`say` + ffmpeg) and committed; CI consumes the committed files.
