"""Speaker diarization: sherpa-onnx engine + attribution math.

Two halves live here. The model-free half is pure and deterministic: it
turns raw diarization output (time ranges + cluster ids) into stable
``S1``/``S2`` labels ordered by first appearance, attributes transcript
segments to speakers by maximum time overlap (whisperX-style,
segment-level), and aggregates per-speaker stats — unit-testable without
models. The engine half wraps ``sherpa-onnx`` (the optional
``[diarization]`` extra) behind ``create_diarizer()`` with a pinned-URL +
sha256 model cache under ``<TALKTHROUGH_HOME>/models/diarization/``; warm
runs never touch the network (local path is checked first, like
``stt._load_model``).

Env knobs (parsed here, consumed by the engine/pipeline):

- ``TALKTHROUGH_DIARIZE=on`` flips the ``process_media`` default; the
  mechanism stays off by default.
- ``TALKTHROUGH_DIARIZATION_THRESHOLD`` — clustering threshold (default 0.5),
  ignored when an explicit ``num_speakers`` is given.
- ``TALKTHROUGH_DIARIZATION_SEG_MODEL`` / ``_EMB_MODEL`` — a name from the
  model allowlist, or a path to a local ``.onnx`` file (offline preseed).
- ``TALKTHROUGH_DIARIZATION_THREADS`` — ONNX threads, default ``min(4, cpus)``,
  applied to BOTH the segmentation and the embedding model.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tarfile
import urllib.request
import wave
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .errors import ToolFailureError, ValidationError
from .stt import SttSegment, SttWord

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

AmendReason = Literal["num_speakers", "embedding_model", "both"]
AMEND_REASONS: frozenset[str] = frozenset({"num_speakers", "embedding_model", "both"})

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.5
MAX_DEFAULT_THREADS = 4

MISSING_EXTRA_REASON = (
    "speaker diarization needs the optional sherpa-onnx engine — reinstall with the "
    "[diarization] extra, e.g. uvx \"talkthrough-mcp[diarization]\""
)


# --- env --------------------------------------------------------------------


def diarize_default() -> bool:
    """Whether ``TALKTHROUGH_DIARIZE`` flips the process default to on."""
    return os.environ.get("TALKTHROUGH_DIARIZE", "off").strip().lower() in {"on", "1", "true"}


def clustering_threshold() -> float:
    raw = os.environ.get("TALKTHROUGH_DIARIZATION_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid TALKTHROUGH_DIARIZATION_THRESHOLD=%r, using %s",
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    if value <= 0:
        # a non-positive distance threshold would go straight into the native
        # clustering with undefined results — same treatment as unparseable
        logger.warning(
            "ignoring non-positive TALKTHROUGH_DIARIZATION_THRESHOLD=%s, using %s",
            value,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    return value


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
    speaker_name_evidence: dict[str, str] | None = None
    # A label-changing amend must deactivate human-verified identities (the
    # new S<n> roster may describe different voices), but it must never erase
    # the user's work. Keep exactly the latest unreviewed mapping as bounded
    # evidence until label_speakers explicitly confirms/removes each label.
    speaker_names_pending_review: dict[str, str] | None = None
    speaker_name_evidence_pending_review: dict[str, str] | None = None
    # Amend outcome metadata; each field stays absent on legacy manifests.
    # ``labels_changed``: set only by the amend path — did the re-run actually
    # relabel anything? ``amend_reason``: which explicit input invalidated the
    # old labels. ``produced_by``: the package version that wrote THIS block;
    # ``tool_versions`` keeps naming what transcribed the job (an amend must
    # not re-stamp it — transcription and diarization provenance differ).
    labels_changed: bool | None = None
    amend_reason: AmendReason | None = None
    produced_by: str | None = None

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
        if self.speaker_name_evidence is not None:
            payload["speaker_name_evidence"] = dict(self.speaker_name_evidence)
        if self.speaker_names_pending_review:
            payload["speaker_names_pending_review"] = dict(
                self.speaker_names_pending_review
            )
            pending_evidence = {
                label: value
                for label, value in (self.speaker_name_evidence_pending_review or {}).items()
                if label in self.speaker_names_pending_review
            }
            if pending_evidence:
                payload["speaker_name_evidence_pending_review"] = pending_evidence
        if self.labels_changed is not None:
            payload["labels_changed"] = self.labels_changed
        if self.amend_reason is not None:
            payload["amend_reason"] = self.amend_reason
        if self.produced_by is not None:
            payload["produced_by"] = self.produced_by
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
        evidence = payload.get("speaker_name_evidence")
        known["speaker_name_evidence"] = (
            {str(k): str(v) for k, v in evidence.items()}
            if isinstance(evidence, dict)
            else None
        )
        pending_names = payload.get("speaker_names_pending_review")
        known["speaker_names_pending_review"] = (
            {str(k): str(v) for k, v in pending_names.items()}
            if isinstance(pending_names, dict) and pending_names
            else None
        )
        pending_evidence = payload.get("speaker_name_evidence_pending_review")
        pending_evidence_clean = (
            {
                str(k): str(v)
                for k, v in pending_evidence.items()
                if str(k) in (known["speaker_names_pending_review"] or {})
            }
            if isinstance(pending_evidence, dict)
            and pending_evidence
            and known["speaker_names_pending_review"]
            else {}
        )
        known["speaker_name_evidence_pending_review"] = pending_evidence_clean or None
        amend_reason = payload.get("amend_reason")
        known["amend_reason"] = amend_reason if amend_reason in AMEND_REASONS else None
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


@dataclass(frozen=True)
class _IntervalLookup:
    """Start-sorted interval index with exact overlap candidate pruning."""

    order: tuple[int, ...]
    starts: tuple[int, ...]
    prefix_max_ends: tuple[int, ...]
    bounds: tuple[tuple[int, int], ...]


def _interval_lookup(bounds: Sequence[tuple[int, int]]) -> _IntervalLookup:
    order = tuple(sorted(range(len(bounds)), key=lambda index: (*bounds[index], index)))
    starts = tuple(bounds[index][0] for index in order)
    prefix: list[int] = []
    highest = -(1 << 63)
    for index in order:
        highest = max(highest, bounds[index][1])
        prefix.append(highest)
    return _IntervalLookup(order, starts, tuple(prefix), tuple(bounds))


def _overlapping_indices(lookup: _IntervalLookup, t0_ms: int, t1_ms: int) -> list[int]:
    """All intervals with positive overlap, without scanning the full timeline."""
    right = bisect_left(lookup.starts, t1_ms)
    found: list[int] = []
    position = right - 1
    while position >= 0 and lookup.prefix_max_ends[position] > t0_ms:
        index = lookup.order[position]
        start, end = lookup.bounds[index]
        if start < t1_ms and end > t0_ms:
            found.append(index)
        position -= 1
    return found


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


def _word_speaker(
    word: SttWord, turns: Sequence[Turn], lookup: _IntervalLookup | None = None
) -> str | None:
    """Best-overlap turn; ties go to earlier turn, then lower S<n>."""
    lookup = lookup or _interval_lookup([(turn.t0_ms, turn.t1_ms) for turn in turns])
    ranked = [
        (
            _overlap_ms(word.t0_ms, word.t1_ms, turns[index].t0_ms, turns[index].t1_ms),
            turns[index],
        )
        for index in _overlapping_indices(lookup, word.t0_ms, word.t1_ms)
    ]
    if not ranked:
        return None
    _shared, winner = min(
        ranked,
        key=lambda item: (-item[0], item[1].t0_ms, _label_number(item[1].speaker)),
    )
    return winner.speaker


def attribute_segments_with_words(
    segments: Sequence[SttSegment],
    words: Sequence[SttWord],
    turns: Sequence[Turn],
) -> list[SttSegment]:
    """Split each Whisper segment at word-level speaker boundaries.

    Words are first assigned to exactly one source segment by maximum time
    overlap, preventing boundary tokens from being duplicated. A source
    segment without a complete usable word list falls back intact to the
    established maximum-overlap segment attribution.
    """
    valid_words: list[SttWord] = []
    invalid_count = 0
    for word in words:
        if word.t0_ms < 0 or word.t1_ms <= word.t0_ms or not word.text.strip():
            invalid_count += 1
        else:
            valid_words.append(word)
    if invalid_count:
        logger.warning(
            "found %d invalid word timestamp(s); using segment-level attribution "
            "for this transcript",
            invalid_count,
        )
        fallback = attribute_segments(segments, turns)
        return [replace(segment, seq=index) for index, segment in enumerate(fallback, start=1)]

    # A previous word-level diarization may already have split one Whisper
    # segment into several stored segments. Coalesce those pieces by their
    # durable source_seq before assigning words again, otherwise every amend
    # could split more finely but could never merge a former boundary.
    source_groups: list[tuple[int, list[SttSegment]]] = []
    for segment in segments:
        source_seq = segment.source_seq if segment.source_seq is not None else segment.seq
        if (
            segment.source_seq is not None
            and source_groups
            and source_groups[-1][0] == source_seq
        ):
            source_groups[-1][1].append(segment)
        else:
            # Segments without source_seq are original/legacy inputs and must
            # remain distinct even if a malformed manifest repeats seq.
            source_groups.append((source_seq, [segment]))

    source_bounds = [
        (
            min(segment.t0_ms for segment in members),
            max(segment.t1_ms for segment in members),
        )
        for _source_seq, members in source_groups
    ]
    segment_lookup = _interval_lookup(source_bounds)
    turn_lookup = _interval_lookup([(turn.t0_ms, turn.t1_ms) for turn in turns])
    by_segment: dict[int, list[SttWord]] = {
        index: [] for index in range(len(source_groups))
    }
    unassigned_count = 0
    for word in valid_words:
        overlapping = [
            (
                _overlap_ms(
                    word.t0_ms,
                    word.t1_ms,
                    source_bounds[index][0],
                    source_bounds[index][1],
                ),
                index,
            )
            for index in _overlapping_indices(segment_lookup, word.t0_ms, word.t1_ms)
        ]
        if not overlapping:
            unassigned_count += 1
            continue
        _shared, chosen = min(
            overlapping,
            key=lambda item: (
                -item[0],
                source_bounds[item[1]][0],
                source_groups[item[1]][0],
            ),
        )
        by_segment[chosen].append(word)

    if unassigned_count:
        logger.warning(
            "found %d word timestamp(s) outside every source segment; using "
            "segment-level attribution for this transcript",
            unassigned_count,
        )
        fallback = attribute_segments(segments, turns)
        return [replace(segment, seq=index) for index, segment in enumerate(fallback, start=1)]

    split: list[SttSegment] = []
    for index, (source_seq, source_members) in enumerate(source_groups):
        segment_words = sorted(by_segment[index], key=lambda word: (word.t0_ms, word.t1_ms))
        if not segment_words:
            split.extend(attribute_segments(source_members, turns))
            continue

        groups: list[tuple[str | None, list[SttWord]]] = []
        for word in segment_words:
            speaker = _word_speaker(word, turns, turn_lookup)
            if groups and groups[-1][0] == speaker:
                groups[-1][1].append(word)
            else:
                groups.append((speaker, [word]))
        for speaker, group_words in groups:
            text = "".join(word.text for word in group_words).strip()
            if not text:
                continue
            split.append(
                SttSegment(
                    seq=0,
                    t0_ms=group_words[0].t0_ms,
                    t1_ms=group_words[-1].t1_ms,
                    text=text,
                    speaker=speaker,
                    source_seq=source_seq,
                )
            )

    return [replace(segment, seq=index) for index, segment in enumerate(split, start=1)]


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


# --- model cache (pinned URLs + sha256, download-once) ------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One downloadable model: pinned release asset + digest + weights license.

    ``archive_member`` names the ``.onnx`` inside a ``.tar.bz2`` asset; None
    means the asset itself is the ``.onnx`` file.
    """

    name: str
    url: str
    sha256: str
    license: str
    archive_member: str | None = None


# The "recongition" typo in the second release tag is real upstream — do not
# "fix" it. All assets live on k2-fsa/sherpa-onnx GitHub releases, ungated.
_SEGMENTATION_TAG = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models"
)
_EMBEDDING_TAG = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
)

SEGMENTATION_MODELS: dict[str, ModelSpec] = {
    "pyannote-segmentation-3-0": ModelSpec(
        name="pyannote-segmentation-3-0",
        url=f"{_SEGMENTATION_TAG}/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        sha256="24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
        license="MIT",
        archive_member="sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    ),
}

EMBEDDING_MODELS: dict[str, ModelSpec] = {
    "wespeaker_en_voxceleb_resnet34_LM": ModelSpec(
        name="wespeaker_en_voxceleb_resnet34_LM",
        url=f"{_EMBEDDING_TAG}/wespeaker_en_voxceleb_resnet34_LM.onnx",
        sha256="e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        license="CC-BY-4.0",
    ),
    "3dspeaker_speech_campplus_sv_en_voxceleb_16k": ModelSpec(
        name="3dspeaker_speech_campplus_sv_en_voxceleb_16k",
        url=f"{_EMBEDDING_TAG}/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
        sha256="357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
        license="Apache-2.0",
    ),
    # NGC model card: "covered by the license of the NeMo Toolkit" → Apache-2.0
    "nemo_en_titanet_small": ModelSpec(
        name="nemo_en_titanet_small",
        url=f"{_EMBEDDING_TAG}/nemo_en_titanet_small.onnx",
        sha256="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
        license="Apache-2.0",
    ),
}

DEFAULT_SEGMENTATION_MODEL = "pyannote-segmentation-3-0"
# Accept-eval winner (v0.2.0, real 3-speaker meeting + RU/EN/ES synthetic set):
# only model to isolate all three real voices at k=3, ~2.2x faster than
# wespeaker (RTF 0.079 vs 0.19 on M-series, 4 threads), Apache-2.0 weights.
DEFAULT_EMBEDDING_MODEL = "nemo_en_titanet_small"


def models_root() -> Path:
    # Imported lazily: jobs -> manifest -> diarize would otherwise be a cycle.
    from .jobs import talkthrough_home

    return talkthrough_home() / "models" / "diarization"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    """One retry on top: a mid-stream stall on a 26-40 MB one-time asset
    shouldn't cost the caller the whole processing run."""
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
                shutil.copyfileobj(response, out)
            return
        except OSError as exc:
            if attempt == 2:
                raise
            logger.warning("download of %s stalled (%s) — retrying once", url, exc)


def _extract_member(archive: Path, member: str, target: Path) -> None:
    """Stream one known member out of a tar.bz2 (no extract() → no path traversal)."""
    staging = target.with_name(target.name + ".part")
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            source = tar.extractfile(member)
        except KeyError:
            source = None
        if source is None:
            raise ToolFailureError(
                f"model archive {archive.name} has no member {member!r} — "
                "delete it and retry to re-download"
            )
        with source, staging.open("wb") as out:
            shutil.copyfileobj(source, out)
    os.replace(staging, target)


def ensure_model_file(spec: ModelSpec) -> Path:
    """Local cache first; download + sha256-verify once on a miss.

    The warm path is a single ``is_file`` check — zero network, the same
    contract the whisper model cache keeps.
    """
    target = models_root() / spec.name / "model.onnx"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    asset_name = spec.url.rsplit("/", 1)[-1]
    logger.info(
        "diarization model %r not in local cache — downloading once from %s "
        "(weights license: %s)",
        spec.name,
        spec.url,
        spec.license,
    )
    download_path = target.parent / (asset_name + ".part")
    try:
        try:
            _download(spec.url, download_path)
        except OSError as exc:
            message = (
                f"could not download diarization model {spec.name!r} from {spec.url}: {exc} — "
                "check network access; the download happens once, warm runs are offline"
            )
            detail = str(exc).lower()
            if "certificate" in detail or "ssl" in detail:
                # the 20-minute investigation a one-liner prevents: corporate
                # TLS interception re-signs traffic with a CA Python's bundled
                # store doesn't trust
                message += (
                    " (corporate TLS interception? point SSL_CERT_FILE at your "
                    "system store — see docs/TROUBLESHOOTING.md)"
                )
            raise ToolFailureError(message) from exc
        digest = _sha256_file(download_path)
        if digest != spec.sha256:
            raise ToolFailureError(
                f"diarization model {asset_name!r} failed sha256 verification "
                f"(expected {spec.sha256}, got {digest}) — retry; if it persists, "
                "report it: the pinned asset should never change"
            )
        if spec.archive_member is None:
            os.replace(download_path, target)
        else:
            _extract_member(download_path, spec.archive_member, target)
    finally:
        with contextlib.suppress(OSError):
            download_path.unlink(missing_ok=True)
    return target


def resolved_embedding_label() -> str:
    """The label ``create_diarizer`` would record for the embedding model —
    WITHOUT touching the network or the disk cache.

    Mirrors ``resolve_model``'s labeling: allowlist name (or the default),
    else the env value read as a local path, verbatim. Used by the amend
    gate to compare against ``diarization.embedding_model`` in a stored
    manifest; an invalid env value simply compares unequal and the engine
    reports the real error when the amend actually runs.
    """
    raw = os.environ.get("TALKTHROUGH_DIARIZATION_EMB_MODEL", "").strip()
    if not raw or raw in EMBEDDING_MODELS:
        return raw or DEFAULT_EMBEDDING_MODEL
    return str(Path(raw).expanduser())


def resolve_model(
    env_var: str, models: Mapping[str, ModelSpec], default_name: str
) -> tuple[str, Path]:
    """Resolve an env override to ``(manifest label, .onnx path)``.

    Accepts a name from the allowlist (cached/downloaded) or a path to a
    local ``.onnx`` file — the offline-preseed escape hatch, used verbatim.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw or raw in models:
        spec = models[raw or default_name]
        return spec.name, ensure_model_file(spec)
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return str(candidate), candidate
    raise ValidationError(
        f"{env_var}={raw!r} is neither a known model name ({', '.join(sorted(models))}) "
        "nor an existing .onnx file"
    )


# --- engine (sherpa-onnx, behind the optional [diarization] extra) ------------


def engine_available() -> bool:
    """Cheap importability probe — lets the pipeline fail fast on an explicit
    diarize request BEFORE spending minutes on whisper."""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    return True


def load_wav_float32(wav_path: Path) -> tuple[NDArray[np.float32], int]:
    """16-bit PCM WAV → float32 samples in [-1, 1] + sample rate.

    Our extracted WAV is already 16 kHz mono s16le — stdlib ``wave`` + numpy
    (transitive via rapidocr) cover it; no librosa/soundfile.
    """
    import numpy as np

    with wave.open(str(wav_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if sample_width != 2:
        raise ToolFailureError(
            f"expected 16-bit PCM WAV for diarization, got sample width {sample_width}"
        )
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return samples, rate


class Diarizer:
    """A loaded sherpa-onnx engine plus the metadata the manifest records.

    Models are loaded once at construction; ``diarize()`` may be called
    repeatedly (the per-call ``num_speakers`` only swaps the clustering
    config, not the loaded models).
    """

    engine = "sherpa-onnx"

    def __init__(
        self,
        sd: Any,
        seg_config: Any,
        emb_config: Any,
        *,
        segmentation_model: str,
        embedding_model: str,
        threshold: float,
        threads: int,
        engine_version: str,
    ) -> None:
        self._sd = sd
        self._seg_config = seg_config
        self._emb_config = emb_config
        self.segmentation_model = segmentation_model
        self.embedding_model = embedding_model
        self.threshold = threshold
        self.threads = threads
        self.engine_version = engine_version

    @property
    def sample_rate(self) -> int:
        return int(self._sd.sample_rate)

    def diarize(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        *,
        num_speakers: int | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[Turn]:
        """Run diarization over mono float32 samples → relabeled, sorted turns.

        ``num_speakers`` > 0 asks the engine to cluster toward k speakers
        (threshold is ignored by the engine then) — a TARGET, not a
        guarantee: the clusterer may converge on fewer clusters than k
        (measured: k=8 and k=9 both yielded 7 on a real meeting). Otherwise
        the threshold decides the speaker count.
        """
        import sherpa_onnx

        if sample_rate != self.sample_rate:
            raise ToolFailureError(
                f"diarization expects {self.sample_rate} Hz audio, got {sample_rate} Hz"
            )
        clustering = sherpa_onnx.FastClusteringConfig(
            num_clusters=int(num_speakers) if num_speakers and num_speakers > 0 else -1,
            threshold=self.threshold,
        )
        self._sd.set_config(
            sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=self._seg_config,
                embedding=self._emb_config,
                clustering=clustering,
            )
        )

        callback = None
        if on_progress is not None:
            progress = on_progress  # narrow for the closure

            def callback(done: int, total: int) -> int:
                progress(done / total if total > 0 else 1.0)
                return 0  # non-zero would abort processing

        result = self._sd.process(samples, callback=callback)
        raw = [
            (int(seg.start * 1000), int(seg.end * 1000), int(seg.speaker))
            for seg in result.sort_by_start_time()
        ]
        return relabel_turns(raw)


def create_diarizer() -> Diarizer | None:
    """Build the engine, or None when the [diarization] extra isn't installed.

    Model resolution may download once (pinned URL + sha256); warm runs are
    zero-network. Raises ``ValidationError`` for a bad
    ``TALKTHROUGH_DIARIZATION_*_MODEL`` override and ``ToolFailureError``
    when a download or the engine setup fails — the pipeline maps those to
    ``diarization.available=false`` instead of losing the transcript.
    """
    try:
        import sherpa_onnx
    except Exception as exc:
        logger.warning("speaker diarization unavailable: %s", exc)
        return None

    seg_label, seg_path = resolve_model(
        "TALKTHROUGH_DIARIZATION_SEG_MODEL", SEGMENTATION_MODELS, DEFAULT_SEGMENTATION_MODEL
    )
    emb_label, emb_path = resolve_model(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
    )
    threads = diarization_threads()
    threshold = clustering_threshold()
    seg_config = sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_path)),
        num_threads=threads,
    )
    emb_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(emb_path), num_threads=threads
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=seg_config,
        embedding=emb_config,
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
    )
    if not config.validate():
        # sherpa logs the specific reason to stderr itself
        raise ToolFailureError(
            "sherpa-onnx rejected the diarization model config — "
            "check the TALKTHROUGH_DIARIZATION_*_MODEL files"
        )
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    import importlib.metadata

    engine_version = ""
    with contextlib.suppress(Exception):  # version probing is best-effort
        engine_version = importlib.metadata.version("sherpa-onnx")
    return Diarizer(
        sd,
        seg_config,
        emb_config,
        segmentation_model=seg_label,
        embedding_model=emb_label,
        threshold=threshold,
        threads=threads,
        engine_version=engine_version,
    )
