"""Manifest schema ``talkthrough-manifest/v1``: build, save/load, and queries.

The manifest is the single durable artifact of a processed job. Everything
the lazy retrieval tools serve (transcript slices, frame lookups, search)
reads from here — the source media is only re-read by ``extract_frame``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .diarize import Diarization, known_fields
from .frames import Frame, frame_floor_s
from .stt import SttSegment
from .wallclock import WallClock

SCHEMA = "talkthrough-manifest/v1"
MANIFEST_NAME = "manifest.json"
FRAMES_DIR_NAME = "frames"


@dataclass(frozen=True)
class MediaMeta:
    path: str
    filename: str
    kind: str  # "video" | "audio"
    duration_s: float
    size_bytes: int
    width: int
    height: int
    video_codec: str
    has_audio: bool
    has_video: bool


@dataclass
class Transcript:
    available: bool
    reason: str
    language: str | None
    model: str | None
    language_probability: float | None = None
    segments: list[SttSegment] = field(default_factory=list)
    diarization: Diarization | None = None

    def full_text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text).strip()


@dataclass
class FrameIndex:
    count: int
    unique_count: int
    cap_hit: bool
    items: list[Frame] = field(default_factory=list)


@dataclass(frozen=True)
class Caps:
    max_seconds: int
    max_frames: int
    scene_threshold: float
    ocr: bool


@dataclass
class Manifest:
    schema: str
    job_id: str
    created_at: str
    media: MediaMeta
    wall_clock: WallClock | None
    transcript: Transcript
    frames: FrameIndex
    caps: Caps
    tool_versions: dict[str, str]

    def t_wall_iso(self, t_ms: int) -> str | None:
        return self.wall_clock.t_wall_iso(t_ms) if self.wall_clock else None

    def unique_frames(self) -> list[Frame]:
        return [frame for frame in self.frames.items if frame.is_unique]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["wall_clock"] = self.wall_clock.to_dict() if self.wall_clock else None
        # Additive diarization fields never serialize as null: non-diarized
        # manifests stay byte-identical to the ones 0.1.x wrote.
        transcript_payload = payload["transcript"]
        for segment_payload in transcript_payload["segments"]:
            if segment_payload.get("speaker") is None:
                del segment_payload["speaker"]
        if self.transcript.diarization is None:
            del transcript_payload["diarization"]
        else:
            transcript_payload["diarization"] = self.transcript.diarization.to_dict()
        return payload

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> Manifest:
        # known_fields() everywhere: unknown keys from NEWER package versions
        # are ignored instead of raising TypeError (additive-schema tolerance).
        media = MediaMeta(**known_fields(MediaMeta, payload["media"]))
        transcript_raw = dict(payload["transcript"])
        transcript_raw["segments"] = [
            SttSegment(**known_fields(SttSegment, segment))
            for segment in transcript_raw.get("segments", [])
        ]
        diarization_raw = transcript_raw.get("diarization")
        transcript_raw["diarization"] = (
            Diarization.from_dict(diarization_raw) if isinstance(diarization_raw, dict) else None
        )
        frames_raw = dict(payload["frames"])
        frames_raw["items"] = [
            Frame(**known_fields(Frame, item)) for item in frames_raw.get("items", [])
        ]
        return Manifest(
            schema=str(payload["schema"]),
            job_id=str(payload["job_id"]),
            created_at=str(payload["created_at"]),
            media=media,
            wall_clock=WallClock.from_dict(payload.get("wall_clock")),
            transcript=Transcript(**known_fields(Transcript, transcript_raw)),
            frames=FrameIndex(**known_fields(FrameIndex, frames_raw)),
            caps=Caps(**known_fields(Caps, payload["caps"])),
            tool_versions={str(k): str(v) for k, v in payload.get("tool_versions", {}).items()},
        )


def save_manifest(manifest: Manifest, job_dir: Path) -> Path:
    path = job_dir / MANIFEST_NAME
    path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_manifest(job_dir: Path) -> Manifest:
    path = job_dir / MANIFEST_NAME
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


# --- transcript formatting -------------------------------------------------


def _srt_timestamp(t_ms: int) -> str:
    hours, rem = divmod(max(0, t_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_srt(segments: list[SttSegment]) -> str:
    """SubRip text: 1-based sequential index, HH:MM:SS,mmm ranges, blank-line separated.

    Diarized segments carry the conventional ``S1: `` speaker prefix in the
    cue text — cues are standalone, so every labeled cue gets one.
    """
    blocks = [
        f"{index}\n{_srt_timestamp(seg.t0_ms)} --> {_srt_timestamp(seg.t1_ms)}\n"
        + (f"{seg.speaker}: {seg.text}" if seg.speaker else seg.text)
        for index, seg in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_text(segments: list[SttSegment]) -> str:
    """Plain prose; diarized runs are prefixed with ``S1: `` at speaker changes."""
    parts: list[str] = []
    current: str | None = None
    for segment in segments:
        if segment.speaker and segment.speaker != current:
            parts.append(f"{segment.speaker}: {segment.text}")
            current = segment.speaker
        else:
            parts.append(segment.text)
    return " ".join(parts)


def slice_segments(
    segments: list[SttSegment], start_ms: int | None, end_ms: int | None
) -> list[SttSegment]:
    """Segments overlapping [start_ms, end_ms] (inclusive bounds, open-ended when None)."""
    lo = start_ms if start_ms is not None else 0
    hi = end_ms if end_ms is not None else None
    picked = []
    for segment in segments:
        if segment.t1_ms < lo:
            continue
        if hi is not None and segment.t0_ms > hi:
            continue
        picked.append(segment)
    return picked


# --- frame queries ----------------------------------------------------------


def _served_frames(manifest: Manifest, include_duplicates: bool) -> list[Frame]:
    return manifest.frames.items if include_duplicates else manifest.unique_frames()


def nearest_frames(
    manifest: Manifest, at_ms: int, count: int, *, include_duplicates: bool = False
) -> list[Frame]:
    """The ``count`` frames closest to ``at_ms``, returned in time order."""
    pool = _served_frames(manifest, include_duplicates)
    closest = sorted(pool, key=lambda frame: (abs(frame.ms - at_ms), frame.ms))[:count]
    return sorted(closest, key=lambda frame: frame.ms)


def frames_in_range(
    manifest: Manifest,
    start_ms: int,
    end_ms: int,
    max_count: int,
    *,
    include_duplicates: bool = False,
) -> list[Frame]:
    """Frames within [start_ms, end_ms], evenly thinned down to ``max_count``."""
    pool = [f for f in _served_frames(manifest, include_duplicates) if start_ms <= f.ms <= end_ms]
    if len(pool) <= max_count:
        return pool
    if max_count <= 1:
        return [pool[len(pool) // 2]]
    step = (len(pool) - 1) / (max_count - 1)
    indices = sorted({round(i * step) for i in range(max_count)})
    return [pool[i] for i in indices]


def representative_frame(manifest: Manifest, at_ms: int) -> Frame | None:
    """The unique frame that best represents the on-screen STATE at ``at_ms``.

    Looks for the time-nearest frame over ALL frames including duplicates: a
    duplicate is proof the screen still looked like its ``duplicate_of``
    keyframe, so it resolves to that unique frame. Plain nearest-unique
    selection can jump across a scene change when a long static stretch was
    deduplicated away (issue #10).
    """
    pool = manifest.frames.items
    if not pool:
        return None
    closest = min(pool, key=lambda frame: (abs(frame.ms - at_ms), frame.ms))
    if closest.duplicate_of is None:
        return closest
    for frame in pool:
        if frame.ms == closest.duplicate_of and frame.duplicate_of is None:
            return frame
    return closest


def nearest_frame_ms(manifest: Manifest, at_ms: int) -> int | None:
    frame = representative_frame(manifest, at_ms)
    return frame.ms if frame else None


def frame_validity_ms(manifest: Manifest, frame: Frame) -> tuple[int, int] | None:
    """``[valid_from_ms, valid_to_ms)`` — when the screen looked like this frame (#14).

    Computed at serve time from the ordered frame list, so existing manifests
    get it for free. A duplicate proves its ``duplicate_of`` keyframe still
    matched the screen, so duplicates share their unique frame's span, and the
    span runs to the NEXT unique frame (exclusive). The last span runs to the
    end of the recording — extraction samples all the way there — unless
    ``cap_hit`` stopped it early, in which case evidence (and the claim) ends
    at the last extracted sample plus one sampling step, never at media end.
    """
    items = manifest.frames.items
    if not items:
        return None
    anchor_ms = frame.ms if frame.duplicate_of is None else frame.duplicate_of
    later_uniques = [f.ms for f in items if f.duplicate_of is None and f.ms > anchor_ms]
    if later_uniques:
        return anchor_ms, min(later_uniques)
    duration_ms = int(manifest.media.duration_s * 1000)
    if not manifest.frames.cap_hit:
        return anchor_ms, max(anchor_ms, duration_ms)
    step_ms = round(frame_floor_s(manifest.media.duration_s, manifest.caps.max_frames) * 1000)
    last_sample_ms = max(item.ms for item in items)
    return anchor_ms, max(anchor_ms, min(duration_ms, last_sample_ms + step_ms))


# --- search -----------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    source: str  # "transcript" | "ocr"
    t_ms: int
    t_wall: str | None
    text: str
    seq: int | None  # transcript segment seq (transcript hits)
    frame_ms: int | None  # frame position (ocr hits)
    nearest_frame_ms: int | None
    speaker: str | None = None  # transcript hits on diarized jobs


def search_manifest(manifest: Manifest, query: str) -> list[SearchHit]:
    """Case-insensitive substring match over transcript segments AND frame OCR text."""
    needle = query.strip().lower()
    hits: list[SearchHit] = []
    if not needle:
        return hits
    for segment in manifest.transcript.segments:
        if needle in segment.text.lower():
            hits.append(
                SearchHit(
                    source="transcript",
                    t_ms=segment.t0_ms,
                    t_wall=manifest.t_wall_iso(segment.t0_ms),
                    text=segment.text,
                    seq=segment.seq,
                    frame_ms=None,
                    nearest_frame_ms=nearest_frame_ms(manifest, segment.t0_ms),
                    speaker=segment.speaker,
                )
            )
    for frame in manifest.frames.items:
        if frame.ocr_text and needle in frame.ocr_text.lower():
            hits.append(
                SearchHit(
                    source="ocr",
                    t_ms=frame.ms,
                    t_wall=manifest.t_wall_iso(frame.ms),
                    text=frame.ocr_text,
                    seq=None,
                    frame_ms=frame.ms,
                    nearest_frame_ms=frame.ms,
                )
            )
    hits.sort(key=lambda hit: hit.t_ms)
    return hits
