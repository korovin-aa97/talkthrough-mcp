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
   │ 1 sha256(file) → job_id      (idempotence: hit → return)  │
   │ 2 ffprobe: streams, duration, tags   → caps + disk check  │
   │ 3 wall-clock resolver (override > qt tag > tag > mtime)   │
   │ 4 ffmpeg → 16 kHz mono WAV → faster-whisper segments      │
   │ 5 ffmpeg ONE pass: select(scene>0.10 ∨ Δt≥1s)             │
   │     + scale ≤1568px + showinfo pts → t<ms>.jpg            │
   │ 6 dHash dedup (consecutive, Hamming ≤4 → duplicate_of)    │
   │ 7 RapidOCR over unique frames → ocr_text                  │
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
| `server.py` | FastMCP app: 7 tools, 5 prompts, progress, MCP error mapping |
| `guidance.py` | Tool descriptions (10-15 examples each) + prompt templates; unit-gated |
| `cli.py` | `serve` (default) / `process` / `gc` |
| `core/pipeline.py` | Orchestrates stages, caps, progress callbacks, summary |
| `core/ffmpeg.py` | Binary ladder: system ffmpeg → `static-ffmpeg` auto-download |
| `core/probe.py` | ffprobe → `MediaInfo` (streams, duration, container tags) |
| `core/wallclock.py` | The resolver ladder + `t_wall` rendering |
| `core/audio.py` / `core/stt.py` | WAV extraction; faster-whisper (CPU int8, VAD) |
| `core/frames.py` | One-pass keyframe extraction + showinfo parsing + exact re-extract |
| `core/dedup.py` | Pillow-only dHash (9×8) + Hamming marking |
| `core/ocr.py` | RapidOCR wrapper; `TALKTHROUGH_OCR=off` or import failure → graceful off |
| `core/manifest.py` | Schema, save/load, SRT, slicing, frame queries, search |
| `core/jobs.py` | Content-addressed store, per-job flock, listing, gc |
| `core/errors.py` | `ValidationError` / `UnknownJobError` / `AudioOnlyJobError` / `ToolFailureError` |

## Job store

```
~/.talkthrough/                  (TALKTHROUGH_HOME overrides)
└── jobs/<sha256(file)[:16]>/
    ├── manifest.json
    ├── frames/t<ms 8-digit>.jpg
    ├── extracts/…               (extract_frame outputs)
    └── job.lock
```

Content addressing makes renames/moves free and `process_media` idempotent:
the second call on the same bytes returns the stored summary in milliseconds.
`force=true` rebuilds (e.g. to re-anchor `recorded_at` or change vocabulary).

## Manifest schema (`talkthrough-manifest/v1`)

```jsonc
{
  "schema": "talkthrough-manifest/v1",
  "job_id": "4d0695c8ab1e38ac",
  "created_at": "2026-07-10T19:32:11+00:00",
  "media": { "path", "filename", "kind": "video|audio", "duration_s",
             "size_bytes", "width", "height", "video_codec",
             "has_audio", "has_video" },
  "wall_clock": { "start_utc", "tz_offset_min", "source", "confidence" } | null,
  "transcript": { "available", "reason", "language", "model",
                  "segments": [{ "seq", "t0_ms", "t1_ms", "text" }] },
  "frames": { "count", "unique_count", "cap_hit",
              "items": [{ "ms", "file", "duplicate_of"?, "ocr_text"? }] },
  "caps": { "max_seconds", "max_frames", "scene_threshold", "ocr" },
  "tool_versions": { "talkthrough-mcp", "ffmpeg", "faster-whisper", "rapidocr" }
}
```

## Wall-clock ladder

| Rung | Source | Confidence | tz offset |
|---|---|---|---|
| 1 | `recorded_at` param | `exact` | from the ISO string (naive → machine-local) |
| 2 | `com.apple.quicktime.creationdate` | `high` | carried by the tag |
| 3 | container `creation_time` | `medium` | unknown (UTC instant only) |
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
  capped at 50 hits.
- Tool descriptions themselves are budgeted: one-line examples, ≤120 chars
  each (gated by `tests/unit/test_guidance.py`).

## Guidance layer

Models use tools far better when the server ships usage guidance:

1. Every tool description embeds 10-15 one-line examples: canonical calls,
   param combos, agent intents mapped to the right call, edge cases
   (audio-only, `wall_clock=null`, truncation continuation), and
   anti-examples redirecting to the better tool.
2. Five `@mcp.prompt()` workflow prompts mirror `examples/prompts/*.md` and
   the Claude Code plugin commands — everything renders from the same
   templates in `guidance.py` via `scripts/gen_integrations.py`, which also
   emits every `integrations/<engine>/` adapter; a unit test byte-pins all
   generated files so they cannot drift.

## Testing strategy

- **unit** — pure logic, no ffmpeg/model downloads: pts regex, wall-clock
  ladder (incl. captured ffprobe JSON), dHash pairs, manifest round-trip,
  SRT, job hashing/gc, the guidance quality gate.
- **integration** — real ffmpeg + whisper `tiny` + RapidOCR over committed
  synthetic fixtures (3-scene screencast with known `creation_time`;
  audio-only meeting): keyword survival, scene-boundary frames, wall-clock
  math, OCR content, dual-source search, caps, idempotence, force re-anchor.
- **e2e** — a real MCP stdio client session against `uv run talkthrough-mcp`:
  discovery (schemas + examples on the wire), prompts, processing, image
  content blocks, search with `t_wall`, SRT.

Fixtures are generated once on macOS by `tests/fixtures/make_fixtures.py`
(`say` + ffmpeg) and committed; CI consumes the committed files.
