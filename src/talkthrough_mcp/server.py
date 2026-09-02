"""MCP server: 8 local tools + 6 workflow prompts, stdio transport.

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

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, guidance
from .core import jobs, pipeline
from .core.diarize import Diarization, speakers_in_range
from .core.errors import AudioOnlyJobError, TalkthroughError, ValidationError
from .core.frames import Frame, extract_exact_frame
from .core.manifest import (
    Manifest,
    format_srt,
    format_text,
    frame_validity_ms,
    frames_in_range,
    nearest_frames,
    representative_frame,
    save_manifest,
    search_manifest,
    slice_segments,
    straddle_hint,
)
from .core.speaker_labels import apply_speaker_label_patch

GET_FRAMES_HARD_CAP = 6
MOMENT_MAX_FRAMES = 3
TRANSCRIPT_CHAR_BUDGET = 30_000  # ~8k tokens
SEARCH_MAX_HITS = 50
LIST_JOBS_MAX = 50

# Non-interactive clients gate tool approvals on these hints (codex exec
# silently cancels un-annotated calls). Both shapes stay honest: nothing here
# destroys user data or reaches beyond the local machine.
READONLY_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
# Writes only inside TALKTHROUGH_HOME (new job dirs / frame extracts);
# content-addressing keeps it idempotent.
LOCAL_WRITE_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

mcp = MCPServer(
    "talkthrough",
    version=__version__,
    instructions=(
        "Local-first recording analysis. Workflow: process_media(path) once per file "
        "(idempotent, content-addressed), then query lazily by job_id — get_transcript "
        "(paginated), search (transcript + on-screen OCR text), get_moment (transcript "
        "slice + frames + OCR for one remark), get_frames (keyframe images), "
        "label_speakers (persist verified S<n>-to-name mappings), extract_frame "
        "(exact-instant full-res re-extract), list_jobs (what is already "
        "processed). Timestamps: t_ms is video-relative; t_wall is real wall-clock time "
        "when the recording start could be resolved. Speaker diarization (optional "
        "[diarization] extra): process_media(diarize=true, num_speakers=N when known) "
        "labels who said what as S1/S2/… across the transcript tools; for any "
        "multi-person recording pass diarize=true as part of normal analysis — do "
        "not wait to be asked who spoke (num_speakers=N whenever the headcount is "
        "known). Server prompts "
        "(bug, triage-recording, spec-from-workshop, backlog-from-demo, "
        "meeting-actions, correlate-with-logs) package the common workflows. "
        # v0.2.3 EXPERIMENT: initialize.instructions is the one text channel
        # not yet falsified for clients that read neither descriptions nor MCP
        # prompts (codex) — the canon-keys sentence rides here, measured by
        # the release battery; harmless for everyone else.
        "Triage findings JSON uses EXACTLY the documented keys (quote, frame_refs, "
        "t_ms, …) — no renames, no wrappers."
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


def _speaker_fields(
    diarization: Diarization | None, label: str | None, *, key: str = "speaker"
) -> dict[str, str]:
    if label is None:
        return {}
    payload = {key: label}
    if diarization is not None:
        name = pipeline.speaker_name(diarization, label)
        if name is not None:
            payload["speaker_name"] = name
    return payload


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
    ctx: Context,
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
        # honesty, not an error (v0.2.6): silent recordings are a headline
        # input, and the `bug` prompt's step 1 sends agents here first — an
        # empty transcript is the truthful answer, in the exact shape of a
        # served one so the calling code never branches
        empty: dict[str, Any] = {
            "job_id": job_id,
            "format": format,
            "language": manifest.transcript.language,
            "media_kind": manifest.media.kind,
            "transcript_available": False,
            "reason": manifest.transcript.reason or "unavailable",
            "segment_count_total": 0,
            "segments_returned": 0,
            "range": {"start_ms": start_ms, "end_ms": end_ms},
            "truncated": False,
            "next_start_ms": None,
            "note": (
                "no audio stream in this recording — narration was never captured; "
                "the frames and on-screen text ARE indexed: use search(job_id, "
                "query=...) for OCR hits and get_frames/get_moment for the visual "
                "timeline"
            ),
        }
        if format == "segments":
            empty["segments"] = []
        elif format == "text":
            empty["text"] = ""
        else:
            empty["srt"] = ""
        return empty
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
        payload["speakers"], hidden = pipeline.roster_payload(diarization, manifest)
        payload["attribution_precision"] = pipeline.attribution_precision(manifest.transcript)
        if payload["attribution_precision"] == "segment":
            payload["attribution_note"] = (
                "speaker boundaries use segment-level timestamps; exact word splitting "
                "requires process_media(..., force=true, diarize=true)"
            )
        if hidden:
            payload["speakers_truncated"] = hidden
        if diarization.labels_changed is not None:
            # additive (v0.2.6): the amend outcome rides the same header the
            # roster does — whether the last re-run relabelled anything must
            # not require re-running process_media to find out
            payload["labels_changed"] = diarization.labels_changed
        if diarization.amend_reason is not None:
            payload["amend_reason"] = diarization.amend_reason
        payload.update(pipeline.pending_review_payload(diarization))
        escalation = pipeline.threshold_escalation_note(diarization)
        if escalation is not None:
            # additive (v0.2.3): the same byte-identical text the process
            # summary carries — agents that start transcript-first (list_jobs
            # → get_transcript) never see that summary
            payload["diarization_note"] = escalation
        noop = pipeline.amend_noop_note(diarization)
        if noop is not None:
            payload["amend_note"] = noop
    if format == "segments":
        payload["segments"] = [
            {
                "seq": segment.seq,
                "t_ms": segment.t0_ms,
                "t_wall": manifest.t_wall_iso(segment.t0_ms),
                **_speaker_fields(diarization, segment.speaker),
                "text": segment.text,
            }
            for segment in served
        ]
    elif format == "text":
        payload["text"] = format_text(served, diarization.speaker_names if diarization else None)
    else:
        payload["srt"] = format_srt(served, diarization.speaker_names if diarization else None)
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
    diarization = manifest.transcript.diarization
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
                **_speaker_fields(diarization, segment.speaker),
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
    if diarization is not None and diarization.available:
        labels = speakers_in_range(diarization.turns, start_ms, end_ms)
        payload["speakers_in_range"] = labels
        named = [_speaker_fields(diarization, label) for label in labels]
        if any("speaker_name" in item for item in named):
            payload["speaker_details_in_range"] = named
    if not manifest.media.has_video:
        payload["note"] = "audio-only job: transcript evidence only, no frames exist"
    elif fallback_note:
        payload["note"] = fallback_note
    directory = jobs.frames_dir(job_id)
    content: list[str | Image] = [_json_block(payload)]
    content.extend(Image(path=directory / frame.file) for frame in picked)
    return content


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["search"], annotations=READONLY_TOOL)
def search(
    job_id: str,
    query: str,
    speaker: str | None = None,
    match_mode: Literal["all_words", "any_word"] = "all_words",
) -> dict[str, Any]:
    manifest = _load(job_id)
    if not query.strip():
        raise ToolError("query is empty — pass a distinctive word or phrase")
    if match_mode not in {"all_words", "any_word"}:
        raise ToolError("match_mode must be 'all_words' or 'any_word'")
    diarization = manifest.transcript.diarization
    speaker_query = speaker.strip() if speaker and speaker.strip() else None
    speaker_labels: list[str] = []
    speaker_value: str | None = None
    if speaker_query is not None:
        if diarization is None or not diarization.available:
            # honesty, not an error: the labels the filter needs don't exist yet
            return {
                "job_id": job_id,
                "query": query,
                **({"match_mode": match_mode} if match_mode != "all_words" else {}),
                "speaker": speaker_query.upper(),
                "hit_count": 0,
                "truncated": False,
                "hits": [],
                "note": (
                    "job is not diarized — speaker labels don't exist here; re-run "
                    "process_media(diarize=true) to add them (only the diarization "
                    "stage runs, transcription is reused), then filter"
                ),
            }
        roster_labels = [stat.label for stat in diarization.speakers]
        canonical_label = speaker_query.upper()
        if canonical_label in roster_labels:
            speaker_labels = [canonical_label]
            speaker_value = canonical_label
        else:
            folded = speaker_query.casefold()
            speaker_labels = [
                label
                for label in roster_labels
                if (diarization.speaker_names or {}).get(label, "").casefold() == folded
            ]
            speaker_value = speaker_query
        if roster_labels and not speaker_labels:
            # honesty again (v0.2.3): a label outside the roster would return
            # a bare empty list indistinguishable from "this voice never said
            # it" — name the mistake and the valid range instead
            span = (
                roster_labels[0]
                if len(roster_labels) == 1
                else f"{roster_labels[0]}-{roster_labels[-1]}"
            )
            looks_like_label = canonical_label.startswith("S") and canonical_label[1:].isdigit()
            if looks_like_label:
                note = (
                    f"label {canonical_label!r} is not in this job's roster ({span}) — "
                    "0 hits here means the label does not exist, not that the words "
                    "were never said; use a roster label or saved speaker name"
                )
            else:
                known_names = sorted(set((diarization.speaker_names or {}).values()))[:12]
                pending_names = diarization.speaker_names_pending_review or {}
                pending_match = any(
                    name.casefold() == folded for name in pending_names.values()
                )
                if pending_match:
                    note = (
                        f"speaker name {speaker_query!r} is saved in pending review because "
                        "diarization changed the labels; it is not an active identity and is "
                        "not used for search. Re-check the current roster and confirm or "
                        "remove it with label_speakers"
                    )
                else:
                    note = (
                        f"speaker name {speaker_query!r} is not saved for this job; roster "
                        f"labels: {span}; known names: {', '.join(known_names) or 'none'} — "
                        "0 hits means the filter is unknown, not that the words were never said"
                    )
            return {
                "job_id": job_id,
                "query": query,
                **({"match_mode": match_mode} if match_mode != "all_words" else {}),
                "speaker": canonical_label if canonical_label.startswith("S") else speaker_query,
                "hit_count": 0,
                "truncated": False,
                "hits": [],
                "note": note,
            }
    if speaker_labels:
        hits = [
            hit
            for label in speaker_labels
            for hit in search_manifest(manifest, query, speaker=label, match_mode=match_mode)
        ]
        hits.sort(key=lambda hit: (hit.t_ms, hit.source, hit.seq or 0))
    else:
        hits = search_manifest(manifest, query, match_mode=match_mode)
    truncated = len(hits) > SEARCH_MAX_HITS
    notes: list[str] = []
    if speaker_labels:
        notes.append(
            "ocr hits are excluded when filtering by speaker — on-screen text has no voice"
        )
        if len(speaker_labels) > 1:
            notes.append(
                f"saved name {speaker_value!r} maps to multiple labels: "
                + ", ".join(speaker_labels)
            )
    if not hits and len(query.split()) >= 2 and match_mode == "all_words":
        # v0.2.3: a zero-hit multi-word query stops being mute. Word-AND is
        # per-segment; the cheap adjacent-pair scan tells apart "phrase
        # straddles a segment boundary" from "words never co-occur".
        straddle_candidates = (
            [straddle_hint(manifest, query, speaker=label) for label in speaker_labels]
            if speaker_labels
            else [straddle_hint(manifest, query)]
        )
        straddle_match = min(
            (value for value in straddle_candidates if value is not None),
            key=lambda value: value.t_ms,
            default=None,
        )
        if straddle_match is not None:
            notes.append(
                f"the words appear together around t_ms={straddle_match.t_ms}, split "
                "across adjacent segments (matching is per-segment); bounded quote: "
                f"{straddle_match.quote!r} — read get_transcript there"
            )
        else:
            notes.append(
                "no single segment contains ALL the words (matching is per-segment, "
                "any order) — drop a word or shorten the stems"
            )
    return {
        "job_id": job_id,
        "query": query,
        **({"match_mode": match_mode} if match_mode != "all_words" else {}),
        **({"speaker": speaker_value} if speaker_value else {}),
        **({"speaker_labels": speaker_labels} if len(speaker_labels) > 1 else {}),
        "hit_count": len(hits),
        "truncated": truncated,
        **({"note": "; ".join(notes)} if notes else {}),
        "hits": [
            {
                "source": hit.source,
                "t_ms": hit.t_ms,
                "t_wall": hit.t_wall,
                **_speaker_fields(diarization, hit.speaker),
                "text": hit.text,
                "segment_seq": hit.seq,
                "frame_ms": hit.frame_ms,
                "nearest_frame_ms": hit.nearest_frame_ms,
            }
            for hit in hits[:SEARCH_MAX_HITS]
        ],
    }


@mcp.tool(
    description=guidance.TOOL_DESCRIPTIONS["label_speakers"],
    annotations=LOCAL_WRITE_TOOL,
)
def label_speakers(
    job_id: str,
    labels: dict[str, str | None],
    evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Persist a validated speaker-name patch under the existing job lock."""
    # Fail unknown ids before job_lock creates its marker directory. The
    # authoritative manifest is still re-read only after the lock below.
    _load(job_id)
    with _tool_errors(), jobs.job_lock(job_id), jobs.partial_job_cleanup(job_id):
        # Re-read only after acquiring the lock: concurrent patches compose
        # instead of overwriting the manifest snapshot seen before the wait.
        manifest = jobs.load_job(job_id)
        diarization = manifest.transcript.diarization
        if diarization is None or not diarization.available:
            raise ValidationError(
                "job is not diarized — run process_media(diarize=true) before labeling speakers"
            )
        apply_speaker_label_patch(diarization, labels, evidence)
        save_manifest(manifest, jobs.job_dir(job_id))

    speakers, hidden = pipeline.roster_payload(diarization, manifest)
    return {
        "job_id": job_id,
        "mapping_count": len(diarization.speaker_names or {}),
        "speakers": speakers,
        **({"speakers_truncated": hidden} if hidden else {}),
        **pipeline.pending_review_payload(diarization),
        "note": (
            "names are user/agent-verified labels; raw S<n> identifiers remain canonical "
            "and OCR name_candidates are only unverified screen hints"
        ),
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


def _job_speakers(diarization: Diarization) -> dict[str, Any]:
    """The list_jobs speaker block: the raw detected count, plus the 30s+
    signal when threshold mode over-detected — a bare ``"speakers": 28`` on
    such jobs reads as a headcount, the exact claim the escalation note
    exists to prevent (the count stays for compatibility)."""
    payload: dict[str, Any] = {"speakers": diarization.detected_num_speakers}
    pending_count = len(diarization.speaker_names_pending_review or {})
    if pending_count:
        payload["speaker_names_pending_review_count"] = pending_count
    if pipeline.threshold_escalation_note(diarization) is not None:
        payload["speakers_with_30s_plus"] = pipeline.substantial_speaker_count(diarization)
    return payload


@mcp.tool(description=guidance.TOOL_DESCRIPTIONS["list_jobs"], annotations=READONLY_TOOL)
def list_jobs() -> dict[str, Any]:
    manifests = jobs.list_jobs()
    return {
        "count": len(manifests),
        "jobs": [
            {
                "job_id": manifest.job_id,
                "media": {"path": manifest.media.path},
                "filename": manifest.media.filename,
                "kind": manifest.media.kind,
                "duration_s": manifest.media.duration_s,
                "created_at": manifest.created_at,
                "wall_clock_start": (
                    manifest.wall_clock.to_dict()["start_utc"] if manifest.wall_clock else None
                ),
                "wall_clock_source": manifest.wall_clock.source if manifest.wall_clock else None,
                "segment_count": len(manifest.transcript.segments),
                # v0.2.6: segment_count alone cannot tell a silent recording
                # from "sound present but nobody spoke" — the flag can
                "has_transcript": manifest.transcript.available,
                "frames_unique": manifest.frames.unique_count,
                "frames_total": manifest.frames.count,
                **(
                    _job_speakers(manifest.transcript.diarization)
                    if manifest.transcript.diarization is not None
                    and manifest.transcript.diarization.available
                    else {}
                ),
            }
            for manifest in manifests[:LIST_JOBS_MAX]
        ],
    }


def _register_prompts() -> None:
    @mcp.prompt(name="bug", description=guidance.PROMPT_DESCRIPTIONS["bug"])
    def bug(job_id: str, product_context: str = "") -> str:
        return guidance.render_prompt("bug", job_id, product_context)

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
