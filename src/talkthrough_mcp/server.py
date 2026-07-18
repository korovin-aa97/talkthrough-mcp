"""FastMCP server: 7 lazy-retrieval tools + 5 workflow prompts, stdio transport.

Design rule: ``process_media`` returns a compact summary, never the full
payload; everything else is lazy and capped. Image responses are MCP image
content (base64 JPEG). All tool descriptions and prompt templates live in
``guidance.py`` — the single, unit-tested source of truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import guidance
from .core import jobs, pipeline
from .core.diarize import speakers_in_range
from .core.errors import AudioOnlyJobError, TalkthroughError
from .core.frames import Frame, extract_exact_frame
from .core.manifest import (
    Manifest,
    format_srt,
    format_text,
    frame_validity_ms,
    frames_in_range,
    nearest_frames,
    representative_frame,
    search_manifest,
    slice_segments,
)

GET_FRAMES_HARD_CAP = 6
MOMENT_MAX_FRAMES = 3
TRANSCRIPT_CHAR_BUDGET = 30_000  # ~8k tokens
SEARCH_MAX_HITS = 50
LIST_JOBS_MAX = 50

# Non-interactive clients gate tool approvals on these hints (codex exec
# silently cancels un-annotated calls). Both shapes stay honest: nothing here
# destroys user data or reaches beyond the local machine.
READONLY_TOOL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Writes only inside TALKTHROUGH_HOME (new job dirs / frame extracts);
# content-addressing keeps it idempotent.
LOCAL_WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

mcp = FastMCP(
    "talkthrough",
    instructions=(
        "Local-first recording analysis. Workflow: process_media(path) once per file "
        "(idempotent, content-addressed), then query lazily by job_id — get_transcript "
        "(paginated), search (transcript + on-screen OCR text), get_moment (transcript "
        "slice + frames + OCR for one remark), get_frames (keyframe images), "
        "extract_frame (exact-instant full-res re-extract), list_jobs (what is already "
        "processed). Timestamps: t_ms is video-relative; t_wall is real wall-clock time "
        "when the recording start could be resolved. Speaker diarization (optional "
        "[diarization] extra): process_media(diarize=true, num_speakers=N when known) "
        "labels who said what as S1/S2/… across the transcript tools; for any "
        "multi-person recording pass diarize=true as part of normal analysis — do "
        "not wait to be asked who spoke (num_speakers=N whenever the headcount is "
        "known). Server prompts "
        "(triage-recording, spec-from-workshop, backlog-from-demo, meeting-actions, "
        "correlate-with-logs) package the common workflows."
    ),
)


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Translate expected pipeline failures into clean MCP tool errors."""
    try:
        yield
    except TalkthroughError as exc:
        raise ToolError(str(exc)) from exc


def _load(job_id: str) -> Manifest:
    with _tool_errors():
        return jobs.load_job(job_id)


def _require_video(manifest: Manifest) -> None:
    if not manifest.media.has_video:
        raise ToolError(str(AudioOnlyJobError(manifest.job_id)))


def _frame_payload(manifest: Manifest, frame: Frame) -> dict[str, Any]:
    payload = {
        "t_ms": frame.ms,
        "t_wall": manifest.t_wall_iso(frame.ms),
        "file": frame.file,
        # absolute path (issue #13): copying the image elsewhere is the
        # calling agent's job, under the user's own permission model
        "path": str((jobs.frames_dir(manifest.job_id) / frame.file).resolve()),
    }
    span = frame_validity_ms(manifest, frame)
    if span is not None:
        # issue #14: the interval during which the screen looked like this
        # keyframe — evidence coverage becomes data, not an inference
        payload["valid_from_ms"], payload["valid_to_ms"] = span
    return payload


def _json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass
class _ProgressState:
    stage: str = "starting"
    fraction: float = 0.0


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["process_media"], annotations=LOCAL_WRITE_TOOL)
async def process_media(
    path: str,
    ctx: Context,  # type: ignore[type-arg]
    recorded_at: str | None = None,
    vocabulary: str | None = None,
    language: str | None = None,
    model: str | None = None,
    diarize: bool | None = None,
    num_speakers: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    state = _ProgressState()
    done = asyncio.Event()

    def on_progress(stage: str, fraction: float) -> None:
        state.stage = stage
        state.fraction = fraction

    async def ticker() -> None:
        last: tuple[str, float] | None = None
        while True:
            current = (state.stage, round(state.fraction, 3))
            if current != last:
                await ctx.report_progress(progress=current[1], total=1.0, message=current[0])
                last = current
            if done.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=1.0)

    await ctx.info(f"processing {path} (local pipeline: ffprobe → whisper → frames → OCR)")
    ticker_task = asyncio.create_task(ticker())
    try:
        with _tool_errors():
            result = await asyncio.to_thread(
                pipeline.process_media,
                path,
                recorded_at=recorded_at,
                vocabulary=vocabulary,
                language=language,
                model=model,
                diarize_speakers=diarize,
                num_speakers=num_speakers,
                force=force,
                progress=on_progress,
            )
    finally:
        done.set()
        await ticker_task
    await ctx.report_progress(progress=1.0, total=1.0, message="done")
    return pipeline.summarize(result)


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["get_transcript"], annotations=READONLY_TOOL)
def get_transcript(
    job_id: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    format: Literal["segments", "text", "srt"] = "segments",
) -> dict[str, Any]:
    manifest = _load(job_id)
    if not manifest.transcript.available:
        raise ToolError(
            f"job {job_id!r} has no transcript ({manifest.transcript.reason or 'unavailable'})"
        )
    picked = slice_segments(manifest.transcript.segments, start_ms, end_ms)

    served: list[Any] = []
    truncated = False
    next_start_ms: int | None = None
    budget = TRANSCRIPT_CHAR_BUDGET
    for segment in picked:
        cost = len(segment.text) + 80  # rough per-segment envelope (ids + timestamps + speaker)
        if budget - cost < 0 and served:
            truncated = True
            next_start_ms = segment.t0_ms
            break
        budget -= cost
        served.append(segment)

    payload: dict[str, Any] = {
        "job_id": job_id,
        "format": format,
        "language": manifest.transcript.language,
        # payload-over-description: an agent writing minutes must not have to
        # remember the media kind — "audio-only" slips on video jobs happen
        "media_kind": manifest.media.kind,
        "segment_count_total": len(manifest.transcript.segments),
        "segments_returned": len(served),
        "range": {"start_ms": start_ms, "end_ms": end_ms},
        "truncated": truncated,
        "next_start_ms": next_start_ms,
    }
    diarization = manifest.transcript.diarization
    if diarization is not None and diarization.available:
        payload["diarized"] = True
        payload["speakers"], hidden = pipeline.roster_payload(diarization)
        if hidden:
            payload["speakers_truncated"] = hidden
    if format == "segments":
        payload["segments"] = [
            {
                "seq": segment.seq,
                "t_ms": segment.t0_ms,
                "t_wall": manifest.t_wall_iso(segment.t0_ms),
                **({"speaker": segment.speaker} if segment.speaker else {}),
                "text": segment.text,
            }
            for segment in served
        ]
    elif format == "text":
        payload["text"] = format_text(served)
    else:
        payload["srt"] = format_srt(served)
    return payload


@mcp.tool(
    description=guidance.TOOL_DESCRIPTIONS["get_frames"],
    annotations=READONLY_TOOL,
    structured_output=False,
)
def get_frames(
    job_id: str,
    at_ms: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    max_frames: int = 4,
    include_duplicates: bool = False,
) -> list[str | Image]:
    manifest = _load(job_id)
    _require_video(manifest)
    ranged = start_ms is not None and end_ms is not None
    if (at_ms is None) == (not ranged):
        raise ToolError(
            "pass either at_ms (nearest frames) or BOTH start_ms and end_ms (range) — "
            "exactly one addressing mode"
        )
    count = max(1, min(max_frames, GET_FRAMES_HARD_CAP))
    if at_ms is not None:
        picked = nearest_frames(manifest, at_ms, count, include_duplicates=include_duplicates)
    else:
        assert start_ms is not None and end_ms is not None
        picked = frames_in_range(
            manifest, start_ms, end_ms, count, include_duplicates=include_duplicates
        )
    directory = jobs.frames_dir(job_id)
    meta = {
        "job_id": job_id,
        "returned": len(picked),
        "max_frames_effective": count,
        "frames": [
            _frame_payload(manifest, frame)
            | ({"ocr_text": frame.ocr_text} if frame.ocr_text else {})
            | ({"duplicate_of": frame.duplicate_of} if frame.duplicate_of is not None else {})
            for frame in picked
        ],
    }
    if not picked:
        meta["note"] = "no frames in the requested range — widen it or use at_ms addressing"
    content: list[str | Image] = [_json_block(meta)]
    content.extend(Image(path=directory / frame.file) for frame in picked)
    return content


@mcp.tool(
    description=guidance.TOOL_DESCRIPTIONS["get_moment"],
    annotations=READONLY_TOOL,
    structured_output=False,
)
def get_moment(job_id: str, start_ms: int, end_ms: int) -> list[str | Image]:
    if end_ms < start_ms:
        raise ToolError(f"end_ms {end_ms} is before start_ms {start_ms}")
    manifest = _load(job_id)
    segments = slice_segments(manifest.transcript.segments, start_ms, end_ms)
    picked = []
    fallback_note: str | None = None
    if manifest.media.has_video:
        picked = frames_in_range(manifest, start_ms, end_ms, MOMENT_MAX_FRAMES)
        if not picked:
            rep = representative_frame(manifest, (start_ms + end_ms) // 2)
            if rep is not None:
                picked = [rep]
                fallback_note = (
                    f"no unique keyframe inside the range — serving t={rep.ms}ms, the "
                    "keyframe representing the on-screen state here (long static "
                    "stretches deduplicate to one keyframe)"
                )
    payload: dict[str, Any] = {
        "job_id": job_id,
        "range": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "t_wall_start": manifest.t_wall_iso(start_ms),
            "t_wall_end": manifest.t_wall_iso(end_ms),
        },
        "transcript": [
            {
                "seq": segment.seq,
                "t_ms": segment.t0_ms,
                "t_wall": manifest.t_wall_iso(segment.t0_ms),
                **({"speaker": segment.speaker} if segment.speaker else {}),
                "text": segment.text,
            }
            for segment in segments
        ],
        "frames": [
            _frame_payload(manifest, frame)
            | ({"ocr_text": frame.ocr_text} if frame.ocr_text else {})
            for frame in picked
        ],
    }
    diarization = manifest.transcript.diarization
    if diarization is not None and diarization.available:
        payload["speakers_in_range"] = speakers_in_range(diarization.turns, start_ms, end_ms)
    if not manifest.media.has_video:
        payload["note"] = "audio-only job: transcript evidence only, no frames exist"
    elif fallback_note:
        payload["note"] = fallback_note
    directory = jobs.frames_dir(job_id)
    content: list[str | Image] = [_json_block(payload)]
    content.extend(Image(path=directory / frame.file) for frame in picked)
    return content


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["search"], annotations=READONLY_TOOL)
def search(job_id: str, query: str, speaker: str | None = None) -> dict[str, Any]:
    manifest = _load(job_id)
    if not query.strip():
        raise ToolError("query is empty — pass a distinctive word or phrase")
    speaker_label = speaker.strip().upper() if speaker and speaker.strip() else None
    if speaker_label is not None:
        diarization = manifest.transcript.diarization
        if diarization is None or not diarization.available:
            # honesty, not an error: the labels the filter needs don't exist yet
            return {
                "job_id": job_id,
                "query": query,
                "speaker": speaker_label,
                "hit_count": 0,
                "truncated": False,
                "hits": [],
                "note": (
                    "job is not diarized — speaker labels don't exist here; re-run "
                    "process_media(diarize=true) to add them (fast amend), then filter"
                ),
            }
    hits = search_manifest(manifest, query, speaker=speaker_label)
    truncated = len(hits) > SEARCH_MAX_HITS
    return {
        "job_id": job_id,
        "query": query,
        **({"speaker": speaker_label} if speaker_label else {}),
        "hit_count": len(hits),
        "truncated": truncated,
        **(
            {
                "note": (
                    "ocr hits are excluded when filtering by speaker — "
                    "on-screen text has no voice"
                )
            }
            if speaker_label
            else {}
        ),
        "hits": [
            {
                "source": hit.source,
                "t_ms": hit.t_ms,
                "t_wall": hit.t_wall,
                **({"speaker": hit.speaker} if hit.speaker else {}),
                "text": hit.text,
                "segment_seq": hit.seq,
                "frame_ms": hit.frame_ms,
                "nearest_frame_ms": hit.nearest_frame_ms,
            }
            for hit in hits[:SEARCH_MAX_HITS]
        ],
    }


@mcp.tool(
    description=guidance.TOOL_DESCRIPTIONS["extract_frame"],
    annotations=LOCAL_WRITE_TOOL,
    structured_output=False,
)
def extract_frame(
    job_id: str,
    at_ms: int,
    crop: dict[str, int] | None = None,
) -> list[str | Image]:
    manifest = _load(job_id)
    _require_video(manifest)
    crop_tuple: tuple[int, int, int, int] | None = None
    if crop is not None:
        missing = {"x", "y", "w", "h"} - crop.keys()
        if missing:
            raise ToolError(f"crop is missing keys: {sorted(missing)} — expected {{x, y, w, h}}")
        if crop["w"] <= 0 or crop["h"] <= 0:
            raise ToolError("crop w and h must be positive")
        crop_tuple = (crop["x"], crop["y"], crop["w"], crop["h"])

    out_dir = jobs.job_dir(job_id) / "extracts"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-crop{'x'.join(str(v) for v in crop_tuple)}" if crop_tuple else ""
    out_path = out_dir / f"extract-t{at_ms:08d}{suffix}.jpg"
    with _tool_errors():
        extract_exact_frame(Path(manifest.media.path), at_ms, out_path, crop=crop_tuple)
    meta = {
        "job_id": job_id,
        "t_ms": at_ms,
        "t_wall": manifest.t_wall_iso(at_ms),
        "source": manifest.media.path,
        "crop": crop,
        "path": str(out_path.resolve()),  # issue #13: agents copy it with their own file tools
        "note": "full source resolution (stored keyframes are capped at 1568px wide)",
    }
    return [_json_block(meta), Image(path=out_path)]


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["list_jobs"], annotations=READONLY_TOOL)
def list_jobs() -> dict[str, Any]:
    manifests = jobs.list_jobs()
    return {
        "count": len(manifests),
        "jobs": [
            {
                "job_id": manifest.job_id,
                "filename": manifest.media.filename,
                "kind": manifest.media.kind,
                "duration_s": manifest.media.duration_s,
                "created_at": manifest.created_at,
                "wall_clock_start": (
                    manifest.wall_clock.to_dict()["start_utc"] if manifest.wall_clock else None
                ),
                "wall_clock_source": manifest.wall_clock.source if manifest.wall_clock else None,
                "segment_count": len(manifest.transcript.segments),
                "frames_unique": manifest.frames.unique_count,
                "frames_total": manifest.frames.count,
                **(
                    {"speakers": manifest.transcript.diarization.detected_num_speakers}
                    if manifest.transcript.diarization is not None
                    and manifest.transcript.diarization.available
                    else {}
                ),
            }
            for manifest in manifests[:LIST_JOBS_MAX]
        ],
    }


def _register_prompts() -> None:
    @mcp.prompt(
        name="triage-recording", description=guidance.PROMPT_DESCRIPTIONS["triage-recording"]
    )
    def triage_recording(job_id: str, product_context: str = "") -> str:
        return guidance.render_prompt("triage-recording", job_id, product_context)

    @mcp.prompt(
        name="spec-from-workshop", description=guidance.PROMPT_DESCRIPTIONS["spec-from-workshop"]
    )
    def spec_from_workshop(job_id: str, feature_name: str = "") -> str:
        return guidance.render_prompt("spec-from-workshop", job_id, feature_name)

    @mcp.prompt(
        name="backlog-from-demo", description=guidance.PROMPT_DESCRIPTIONS["backlog-from-demo"]
    )
    def backlog_from_demo(job_id: str, project_context: str = "") -> str:
        return guidance.render_prompt("backlog-from-demo", job_id, project_context)

    @mcp.prompt(
        name="meeting-actions", description=guidance.PROMPT_DESCRIPTIONS["meeting-actions"]
    )
    def meeting_actions(job_id: str, attendees: str = "") -> str:
        return guidance.render_prompt("meeting-actions", job_id, attendees)

    @mcp.prompt(
        name="correlate-with-logs",
        description=guidance.PROMPT_DESCRIPTIONS["correlate-with-logs"],
    )
    def correlate_with_logs(job_id: str, log_source: str = "") -> str:
        return guidance.render_prompt("correlate-with-logs", job_id, log_source)


_register_prompts()
