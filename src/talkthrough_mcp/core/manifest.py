"""Manifest schema ``talkthrough-manifest/v1``: build, save/load, and queries.

The manifest is the single durable artifact of a processed job. Everything
the lazy retrieval tools serve (transcript slices, frame lookups, search)
reads from here — the source media is only re-read by ``extract_frame``.
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .diarize import Diarization, known_fields
from .frames import Frame, frame_floor_s
from .stt import SttSegment, SttWord
from .wallclock import WallClock

SCHEMA = "talkthrough-manifest/v1"
MANIFEST_NAME = "manifest.json"
FRAMES_DIR_NAME = "frames"


@dataclass(frozen=True)
class MediaOrigin:
    """Where a managed source came from — bounded, secret-free provider facts.

    The raw URL is never stored: only a one-way hash of the exact input
    (``url_sha256``, for the URL index), the public provider id or host, and
    provider publication time kept deliberately apart from ``wall_clock``
    (an upload date is not a recording start).
    """

    kind: str  # "youtube" | "direct_url"
    provider: str  # "youtube" | the direct host
    url_sha256: str
    provider_id: str | None = None
    host: str | None = None
    title: str | None = None
    published_at: str | None = None
    downloader: str | None = None
    downloaded_bytes: int | None = None
    downloaded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @staticmethod
    def from_dict(payload: object) -> MediaOrigin | None:
        if not isinstance(payload, dict):
            return None
        known = known_fields(MediaOrigin, payload)
        if not isinstance(known.get("kind"), str) or not isinstance(known.get("provider"), str):
            return None
        if not isinstance(known.get("url_sha256"), str):
            return None
        for key in ("provider_id", "host", "title", "published_at", "downloader", "downloaded_at"):
            if key in known and known[key] is not None and not isinstance(known[key], str):
                known[key] = str(known[key])
        size = known.get("downloaded_bytes")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            known["downloaded_bytes"] = None
        return MediaOrigin(**known)


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
    # Additive (0.4.0): a source Talkthrough downloaded itself lives inside the
    # job directory at ``managed_source`` (relative path) and carries its
    # ``origin``; both stay absent on local-file jobs so older manifests and
    # non-URL jobs serialize byte-identically.
    origin: MediaOrigin | None = None
    managed_source: str | None = None


@dataclass
class Transcript:
    available: bool
    reason: str
    language: str | None
    model: str | None
    language_probability: float | None = None
    segments: list[SttSegment] = field(default_factory=list)
    words: list[SttWord] | None = None
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
        # Additive media/diarization fields never serialize as null: local-file
        # and non-diarized manifests stay byte-identical to the ones 0.1.x wrote.
        media_payload = payload["media"]
        if self.media.origin is None:
            del media_payload["origin"]
        else:
            media_payload["origin"] = self.media.origin.to_dict()
        if self.media.managed_source is None:
            del media_payload["managed_source"]
        transcript_payload = payload["transcript"]
        for segment_payload in transcript_payload["segments"]:
            if segment_payload.get("speaker") is None:
                del segment_payload["speaker"]
            if segment_payload.get("source_seq") is None:
                del segment_payload["source_seq"]
        if self.transcript.words is None:
            del transcript_payload["words"]
        else:
            transcript_payload["words"] = [
                [word.t0_ms, word.t1_ms, word.text] for word in self.transcript.words
            ]
        if self.transcript.diarization is None:
            del transcript_payload["diarization"]
        else:
            transcript_payload["diarization"] = self.transcript.diarization.to_dict()
        return payload

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> Manifest:
        # known_fields() everywhere: unknown keys from NEWER package versions
        # are ignored instead of raising TypeError (additive-schema tolerance).
        media_raw = known_fields(MediaMeta, payload["media"])
        media_raw["origin"] = MediaOrigin.from_dict(media_raw.get("origin"))
        managed = media_raw.get("managed_source")
        media_raw["managed_source"] = managed if isinstance(managed, str) and managed else None
        media = MediaMeta(**media_raw)
        transcript_raw = dict(payload["transcript"])
        transcript_raw["segments"] = [
            SttSegment(**known_fields(SttSegment, segment))
            for segment in transcript_raw.get("segments", [])
        ]
        words_raw = transcript_raw.get("words")
        transcript_raw["words"] = (
            [
                SttWord(t0_ms=int(item[0]), t1_ms=int(item[1]), text=str(item[2]))
                for item in words_raw
                if isinstance(item, (list, tuple)) and len(item) == 3
            ]
            if isinstance(words_raw, list)
            else None
        )
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


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically from a same-directory temp file (fsynced)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def save_manifest(manifest: Manifest, job_dir: Path) -> Path:
    """Atomically replace the durable manifest from a same-directory temp file."""
    path = job_dir / MANIFEST_NAME
    atomic_write_text(path, _encode_manifest(manifest))
    return path


def _encode_manifest(manifest: Manifest) -> str:
    """Keep the human-readable manifest while encoding the large word array compactly."""
    payload = manifest.to_dict()
    transcript = payload.get("transcript")
    if not isinstance(transcript, dict) or "words" not in transcript:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    words = transcript["words"]
    # ``json`` has no public raw-fragment hook. Substitute a deterministic,
    # collision-checked marker after the normal pretty render so every word
    # triplet stays on one physical line instead of expanding to five.
    suffix = 0
    while True:
        marker = f"__talkthrough_compact_words_{suffix}__"
        transcript["words"] = marker
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        marker_json = json.dumps(marker, ensure_ascii=False)
        if encoded.count(marker_json) == 1:
            break
        suffix += 1
    compact_words = json.dumps(words, ensure_ascii=False, separators=(",", ":"))
    return encoded.replace(marker_json, compact_words, 1)


def load_manifest(job_dir: Path) -> Manifest:
    path = job_dir / MANIFEST_NAME
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


# --- transcript formatting -------------------------------------------------


def _srt_timestamp(t_ms: int) -> str:
    hours, rem = divmod(max(0, t_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _speaker_display(label: str, speaker_names: dict[str, str] | None) -> str:
    name = speaker_names.get(label) if speaker_names else None
    return f"{name} ({label})" if name else label


def format_srt(
    segments: list[SttSegment], speaker_names: dict[str, str] | None = None
) -> str:
    """SubRip text: 1-based sequential index, HH:MM:SS,mmm ranges, blank-line separated.

    Diarized segments carry the conventional ``S1: `` speaker prefix in the
    cue text — cues are standalone, so every labeled cue gets one.
    """
    blocks = [
        f"{index}\n{_srt_timestamp(seg.t0_ms)} --> {_srt_timestamp(seg.t1_ms)}\n"
        + (
            f"{_speaker_display(seg.speaker, speaker_names)}: {seg.text}"
            if seg.speaker
            else seg.text
        )
        for index, seg in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_text(
    segments: list[SttSegment], speaker_names: dict[str, str] | None = None
) -> str:
    """Plain prose; diarized runs are prefixed with ``S1: `` at speaker changes."""
    parts: list[str] = []
    current: str | None = None
    for segment in segments:
        if segment.speaker and segment.speaker != current:
            parts.append(f"{_speaker_display(segment.speaker, speaker_names)}: {segment.text}")
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


@dataclass(frozen=True)
class StraddleHint:
    """Bounded evidence for a query split across adjacent segments."""

    t_ms: int
    quote: str
    all_tokens_shown: bool = True


STRADDLE_QUOTE_MAX_CHARS = 240


def _fold_for_search(text: str) -> str:
    """Match-time normalization applied to BOTH query and indexed text (#16).

    NFC first (so a decomposed ``е`` + combining diaeresis becomes ``ё``
    before folding), then casefold, then ё→е — Russian writes the same word
    both ways and neither side should have to guess which one the recording
    used.
    """
    return unicodedata.normalize("NFC", text).casefold().replace("ё", "е")


def _word_token_positions(words: list[str], tokens: list[str]) -> dict[str, list[int]]:
    """Map folded query tokens to the display words containing them."""
    folded_words = [_fold_for_search(word) for word in words]
    return {
        token: [index for index, word in enumerate(folded_words) if token in word]
        for token in tokens
    }


def _word_window(words: list[str], anchors: list[int]) -> str:
    """Smallest whole-word excerpt covering anchors, with explicit crop marks."""
    start = min(anchors)
    end = max(anchors)
    core = " ".join(words[start : end + 1])
    return ("… " if start else "") + core + (" …" if end < len(words) - 1 else "")


def _anchor_sample(word: str, token: str, budget: int) -> str:
    """Deterministic fallback for an objectively over-budget anchor word."""
    if len(word) <= budget:
        return word
    content_budget = max(1, budget - 2)
    folded = _fold_for_search(word)
    token_at = max(0, folded.find(token))
    start = max(0, min(len(word) - content_budget, token_at - content_budget // 2))
    sample = word[start : start + content_budget]
    return ("…" if start else "") + sample + ("…" if start + content_budget < len(word) else "")


def _straddle_quote(first_text: str, second_text: str, tokens: list[str]) -> tuple[str, bool]:
    """Build the smallest query-aware quote that proves both sides of a boundary."""
    first_words = first_text.split()
    second_words = second_text.split()
    full = " ".join([*first_words, *second_words])
    if len(full) <= STRADDLE_QUOTE_MAX_CHARS:
        return full, True

    first_positions = _word_token_positions(first_words, tokens)
    second_positions = _word_token_positions(second_words, tokens)
    first_anchors: list[int] = []
    second_anchors: list[int] = []
    for token in tokens:
        in_first = first_positions[token]
        in_second = second_positions[token]
        # Shared tokens resolve to the first segment deterministically. Tokens
        # unique to either side always anchor that side, which is the proof a
        # prefix-only truncation used to erase.
        if in_first:
            first_anchors.append(in_first[0])
        elif in_second:
            second_anchors.append(in_second[0])

    # A true cross-boundary match necessarily contributes at least one token
    # from each side. Keep defensive fallbacks for unusual Unicode splitting.
    first_anchors = first_anchors or [len(first_words) - 1]
    second_anchors = second_anchors or [0]
    first_window = _word_window(first_words, first_anchors)
    second_window = _word_window(second_words, second_anchors)
    if first_window.endswith(" …") and second_window.startswith("… "):
        # Both independently cropped windows mark the same segment boundary.
        # Render that boundary once instead of the noisy ``… …`` join.
        quote = f"{first_window[:-2]} … {second_window[2:]}"
    else:
        quote = f"{first_window} {second_window}"
    if len(quote) <= STRADDLE_QUOTE_MAX_CHARS:
        folded_quote = _fold_for_search(quote)
        return quote, all(token in folded_quote for token in tokens)

    # When all token anchors are farther apart than the total budget, show one
    # deterministic exclusive anchor from each segment and let the caller say
    # explicitly that this is a sample rather than proof of every token.
    first_only = next(
        token for token in tokens if first_positions[token] and not second_positions[token]
    )
    second_only = next(
        token for token in tokens if second_positions[token] and not first_positions[token]
    )
    side_budget = (STRADDLE_QUOTE_MAX_CHARS - 1) // 2
    first_sample = _anchor_sample(
        first_words[first_positions[first_only][0]], first_only, side_budget
    )
    second_sample = _anchor_sample(
        second_words[second_positions[second_only][0]], second_only, side_budget
    )
    return f"{first_sample} {second_sample}"[:STRADDLE_QUOTE_MAX_CHARS], False


def search_manifest(
    manifest: Manifest,
    query: str,
    *,
    speaker: str | None = None,
    match_mode: Literal["all_words", "any_word"] = "all_words",
) -> list[SearchHit]:
    """Word-level match over transcript segments AND frame OCR text.

    The query is tokenized on whitespace. ``all_words`` preserves the
    existing contract: every token must match as a substring, in any order
    and at any distance. ``any_word`` matches when at least one token does.
    A single-word query therefore behaves exactly like the old
    exact-substring match in either mode.
    ``speaker`` filters transcript hits to one diarized label; OCR hits are
    excluded then (on-screen text has no voice).
    """
    tokens = _fold_for_search(query).split()
    hits: list[SearchHit] = []
    if not tokens:
        return hits
    if match_mode not in {"all_words", "any_word"}:
        raise ValueError(f"unknown match_mode: {match_mode}")

    def matches(text: str) -> bool:
        folded = _fold_for_search(text)
        matched = (token in folded for token in tokens)
        return any(matched) if match_mode == "any_word" else all(matched)

    for segment in manifest.transcript.segments:
        if speaker is not None and segment.speaker != speaker:
            continue
        if matches(segment.text):
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
    if speaker is not None:
        hits.sort(key=lambda hit: hit.t_ms)
        return hits
    for frame in manifest.frames.items:
        if frame.ocr_text and matches(frame.ocr_text):
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


def straddle_hint(
    manifest: Manifest, query: str, *, speaker: str | None = None
) -> StraddleHint | None:
    """First adjacent segment pair whose combined text matches every token.

    Word-AND is per-segment BY CONTRACT (#16); a phrase split over a segment
    boundary ("recurring | invites") legitimately misses, and the note should
    say where the words do meet and show a bounded quote instead of leaving
    the miss indistinguishable from "never said". Same normalization as the
    search itself. OCR text stays out — frames are not contiguous prose. With
    ``speaker`` both segments of the pair must be that speaker's, mirroring
    the filter the zero-hit search ran under. Not a search mode: this feeds
    one prose note.
    """
    tokens = _fold_for_search(query).split()
    if len(tokens) < 2:
        return None
    for first, second in itertools.pairwise(manifest.transcript.segments):
        if speaker is not None and (first.speaker != speaker or second.speaker != speaker):
            continue
        first_folded = _fold_for_search(first.text)
        second_folded = _fold_for_search(second.text)
        combined = f"{first_folded} {second_folded}"
        if (
            all(token in combined for token in tokens)
            and not all(token in first_folded for token in tokens)
            and not all(token in second_folded for token in tokens)
        ):
            quote, all_tokens_shown = _straddle_quote(first.text, second.text, tokens)
            return StraddleHint(
                t_ms=first.t0_ms,
                quote=quote,
                all_tokens_shown=all_tokens_shown,
            )
    return None


def straddle_hint_t_ms(
    manifest: Manifest, query: str, *, speaker: str | None = None
) -> int | None:
    """Compatibility helper returning only the adjacent-pair timestamp."""
    hint = straddle_hint(manifest, query, speaker=speaker)
    return hint.t_ms if hint is not None else None
