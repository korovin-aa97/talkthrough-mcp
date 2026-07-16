"""Speaker diarization: turn relabeling, segment attribution, roster math.

Everything in this module is model-free and deterministic: it turns raw
diarization output (time ranges + cluster ids) into stable ``S1``/``S2``
labels ordered by first appearance, attributes transcript segments to
speakers by maximum time overlap (whisperX-style, segment-level), and
aggregates per-speaker stats. The sherpa-onnx engine wrapper and the pinned
model cache land in this module in a follow-up commit of the same PR
(issue #4); keeping the math pure keeps it unit-testable without models.

Env knobs (parsed here, consumed by the engine/pipeline):

- ``TALKTHROUGH_DIARIZE=on`` flips the ``process_media`` default; the
  mechanism stays off by default.
- ``TALKTHROUGH_DIARIZATION_THRESHOLD`` — clustering threshold (default 0.5),
  ignored when an explicit ``num_speakers`` is given.
- ``TALKTHROUGH_DIARIZATION_THREADS`` — ONNX threads, default ``min(4, cpus)``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from typing import Any

from .stt import SttSegment

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.5
MAX_DEFAULT_THREADS = 4


# --- env --------------------------------------------------------------------


def diarize_default() -> bool:
    """Whether ``TALKTHROUGH_DIARIZE`` flips the process default to on."""
    return os.environ.get("TALKTHROUGH_DIARIZE", "off").strip().lower() in {"on", "1", "true"}


def clustering_threshold() -> float:
    raw = os.environ.get("TALKTHROUGH_DIARIZATION_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid TALKTHROUGH_DIARIZATION_THRESHOLD=%r, using %s",
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD


def diarization_threads() -> int:
    default = min(MAX_DEFAULT_THREADS, os.cpu_count() or 1)
    raw = os.environ.get("TALKTHROUGH_DIARIZATION_THREADS", "").strip()
    if not raw:
        return default
    try:
        threads = int(raw)
    except ValueError:
        logger.warning("ignoring invalid TALKTHROUGH_DIARIZATION_THREADS=%r", raw)
        return default
    if threads < 1:
        logger.warning("ignoring TALKTHROUGH_DIARIZATION_THREADS=%d (must be >= 1)", threads)
        return default
    return threads


# --- data model ---------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One diarized speech turn; ``speaker`` is a stable ``S<n>`` label."""

    t0_ms: int
    t1_ms: int
    speaker: str


@dataclass(frozen=True)
class SpeakerStat:
    label: str
    talk_time_ms: int
    turn_count: int
    first_ms: int
    last_ms: int


@dataclass
class Diarization:
    """Additive manifest block under ``transcript`` (schema stays v1).

    ``turns`` serialize as compact ``[t0_ms, t1_ms, "S1"]`` triplets — they
    are kept for range queries (``get_moment``) and future word-level
    splitting without re-diarizing; disk cost, not context cost.
    """

    available: bool
    reason: str
    engine: str | None = None
    engine_version: str | None = None
    segmentation_model: str | None = None
    embedding_model: str | None = None
    requested_num_speakers: int | None = None
    detected_num_speakers: int | None = None
    threshold: float | None = None
    speakers: list[SpeakerStat] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    speaker_names: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "available": self.available,
            "reason": self.reason,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "segmentation_model": self.segmentation_model,
            "embedding_model": self.embedding_model,
            "requested_num_speakers": self.requested_num_speakers,
            "detected_num_speakers": self.detected_num_speakers,
            "threshold": self.threshold,
            "speakers": [
                {
                    "label": stat.label,
                    "talk_time_ms": stat.talk_time_ms,
                    "turn_count": stat.turn_count,
                    "first_ms": stat.first_ms,
                    "last_ms": stat.last_ms,
                }
                for stat in self.speakers
            ],
            "turns": [[turn.t0_ms, turn.t1_ms, turn.speaker] for turn in self.turns],
        }
        if self.speaker_names is not None:
            payload["speaker_names"] = dict(self.speaker_names)
        return payload

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> Diarization:
        known = known_fields(Diarization, payload)
        known["speakers"] = [
            SpeakerStat(**known_fields(SpeakerStat, stat))
            for stat in payload.get("speakers", [])
        ]
        known["turns"] = [
            Turn(t0_ms=int(item[0]), t1_ms=int(item[1]), speaker=str(item[2]))
            for item in payload.get("turns", [])
            if isinstance(item, (list, tuple)) and len(item) == 3
        ]
        names = payload.get("speaker_names")
        known["speaker_names"] = (
            {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else None
        )
        return Diarization(**known)


def known_fields(cls: type, payload: dict[str, Any]) -> dict[str, Any]:
    """Drop keys a dataclass doesn't know — manifests from newer versions load."""
    allowed = {f.name for f in fields(cls)}
    return {key: value for key, value in payload.items() if key in allowed}


# --- pure math ----------------------------------------------------------------


def _label_number(label: str) -> int:
    try:
        return int(label.lstrip("S"))
    except ValueError:
        return 1 << 30


def relabel_turns(raw_turns: Sequence[tuple[int, int, int]]) -> list[Turn]:
    """Map raw ``(t0_ms, t1_ms, cluster_id)`` turns onto ``S1``/``S2``/… labels.

    Labels are assigned by FIRST APPEARANCE in time order, so the same audio
    always yields the same labels regardless of engine cluster numbering.
    """
    ordered = sorted(raw_turns, key=lambda t: (t[0], t[1]))
    labels: dict[int, str] = {}
    turns: list[Turn] = []
    for t0_ms, t1_ms, cluster_id in ordered:
        if cluster_id not in labels:
            labels[cluster_id] = f"S{len(labels) + 1}"
        turns.append(Turn(t0_ms=int(t0_ms), t1_ms=int(t1_ms), speaker=labels[cluster_id]))
    return turns


def _overlap_ms(t0_a: int, t1_a: int, t0_b: int, t1_b: int) -> int:
    return max(0, min(t1_a, t1_b) - max(t0_a, t0_b))


def attribute_segments(
    segments: Sequence[SttSegment], turns: Sequence[Turn]
) -> list[SttSegment]:
    """Assign each segment the speaker with the largest total time overlap.

    whisperX-style, segment-level: overlaps are summed per speaker across all
    of that speaker's turns. No overlap at all → ``speaker=None``. Exact ties
    go to the earlier label (lower ``S<n>``). Always recomputes — stale labels
    from a previous run are overwritten (amend path re-attributes in place).
    """
    attributed: list[SttSegment] = []
    for segment in segments:
        totals: dict[str, int] = {}
        for turn in turns:
            shared = _overlap_ms(segment.t0_ms, segment.t1_ms, turn.t0_ms, turn.t1_ms)
            if shared > 0:
                totals[turn.speaker] = totals.get(turn.speaker, 0) + shared
        winner = (
            min(totals, key=lambda label: (-totals[label], _label_number(label)))
            if totals
            else None
        )
        attributed.append(replace(segment, speaker=winner))
    return attributed


def speaker_roster(turns: Sequence[Turn]) -> list[SpeakerStat]:
    """Per-speaker aggregates, ordered by label number (== first appearance)."""
    stats: dict[str, dict[str, int]] = {}
    for turn in turns:
        entry = stats.setdefault(
            turn.speaker,
            {"talk_time_ms": 0, "turn_count": 0, "first_ms": turn.t0_ms, "last_ms": turn.t1_ms},
        )
        entry["talk_time_ms"] += max(0, turn.t1_ms - turn.t0_ms)
        entry["turn_count"] += 1
        entry["first_ms"] = min(entry["first_ms"], turn.t0_ms)
        entry["last_ms"] = max(entry["last_ms"], turn.t1_ms)
    return [
        SpeakerStat(
            label=label,
            talk_time_ms=stats[label]["talk_time_ms"],
            turn_count=stats[label]["turn_count"],
            first_ms=stats[label]["first_ms"],
            last_ms=stats[label]["last_ms"],
        )
        for label in sorted(stats, key=_label_number)
    ]


def speakers_in_range(turns: Sequence[Turn], start_ms: int, end_ms: int) -> list[str]:
    """Labels of speakers whose turns overlap [start_ms, end_ms], by label order.

    Inclusive bounds, mirroring ``manifest.slice_segments`` — a turn touching
    the range boundary counts, so ``get_moment`` lists every speaker whose
    segments it serves.
    """
    present = {
        turn.speaker for turn in turns if turn.t1_ms >= start_ms and turn.t0_ms <= end_ms
    }
    return sorted(present, key=_label_number)
