"""Attribution math, label determinism, roster/range queries, env parsing."""

from __future__ import annotations

import pytest

from talkthrough_mcp.core.diarize import (
    DEFAULT_THRESHOLD,
    Diarization,
    SpeakerStat,
    Turn,
    attribute_segments,
    clustering_threshold,
    diarization_threads,
    diarize_default,
    relabel_turns,
    speaker_roster,
    speakers_in_range,
)
from talkthrough_mcp.core.stt import SttSegment


def seg(seq: int, t0_ms: int, t1_ms: int, speaker: str | None = None) -> SttSegment:
    return SttSegment(seq=seq, t0_ms=t0_ms, t1_ms=t1_ms, text=f"segment {seq}", speaker=speaker)


# --- relabel_turns ------------------------------------------------------------


def test_relabel_orders_labels_by_first_appearance_not_cluster_id() -> None:
    turns = relabel_turns([(0, 1000, 7), (1500, 2500, 3), (3000, 4000, 7)])
    assert turns == [
        Turn(0, 1000, "S1"),
        Turn(1500, 2500, "S2"),
        Turn(3000, 4000, "S1"),
    ]


def test_relabel_sorts_unordered_input_by_time() -> None:
    turns = relabel_turns([(5000, 6000, 1), (0, 1000, 2)])
    assert [turn.speaker for turn in turns] == ["S1", "S2"]
    assert turns[0].t0_ms == 0


def test_relabel_supports_double_digit_speakers() -> None:
    raw = [(i * 1000, i * 1000 + 500, i) for i in range(12)]
    turns = relabel_turns(raw)
    assert turns[9].speaker == "S10"
    assert turns[11].speaker == "S12"


# --- attribute_segments -------------------------------------------------------


def test_attribution_full_cover_single_speaker() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 1000, 3000)], turns)
    assert attributed.speaker == "S1"
    assert attributed.text == "segment 1"  # everything else untouched


def test_attribution_partial_overlap_picks_larger_share() -> None:
    turns = [Turn(0, 4000, "S1"), Turn(4000, 10_000, "S2")]
    (attributed,) = attribute_segments([seg(1, 3000, 8000)], turns)
    assert attributed.speaker == "S2"  # 1s of S1 vs 4s of S2


def test_attribution_sums_multiple_turns_of_same_speaker() -> None:
    # S1 speaks 0-3s and 7-10s (6s total) around S2's single 4s turn.
    turns = [Turn(0, 3000, "S1"), Turn(3000, 7000, "S2"), Turn(7000, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 0, 10_000)], turns)
    assert attributed.speaker == "S1"


def test_attribution_no_overlap_is_none() -> None:
    turns = [Turn(0, 1000, "S1")]
    (attributed,) = attribute_segments([seg(1, 5000, 6000)], turns)
    assert attributed.speaker is None


def test_attribution_exact_tie_goes_to_lower_label() -> None:
    turns = [Turn(0, 5000, "S2"), Turn(5000, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 0, 10_000)], turns)
    assert attributed.speaker == "S1"


def test_attribution_overwrites_stale_labels() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (relabeled,) = attribute_segments([seg(1, 0, 2000, speaker="S9")], turns)
    assert relabeled.speaker == "S1"
    (cleared,) = attribute_segments([seg(1, 0, 2000, speaker="S9")], [])
    assert cleared.speaker is None


def test_attribution_zero_length_segment_is_none() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 500, 500)], turns)
    assert attributed.speaker is None


def test_attribution_keeps_segment_order_and_count() -> None:
    turns = [Turn(0, 2000, "S1"), Turn(2000, 4000, "S2")]
    segments = [seg(1, 0, 1500), seg(2, 2200, 3800), seg(3, 9000, 9500)]
    attributed = attribute_segments(segments, turns)
    assert [s.seq for s in attributed] == [1, 2, 3]
    assert [s.speaker for s in attributed] == ["S1", "S2", None]


# --- roster / ranges ----------------------------------------------------------


def test_roster_aggregates_and_orders_numerically() -> None:
    turns = relabel_turns([(i * 1000, i * 1000 + 500, i) for i in range(11)])
    turns.append(Turn(20_000, 21_000, "S1"))
    roster = speaker_roster(turns)
    assert [stat.label for stat in roster][:3] == ["S1", "S2", "S3"]
    assert roster[-1].label == "S11"  # numeric order, not lexicographic
    s1 = roster[0]
    assert s1 == SpeakerStat(
        label="S1", talk_time_ms=1500, turn_count=2, first_ms=0, last_ms=21_000
    )


def test_speakers_in_range_inclusive_bounds_like_slice_segments() -> None:
    turns = [Turn(0, 1000, "S1"), Turn(1000, 2000, "S2"), Turn(5000, 6000, "S3")]
    assert speakers_in_range(turns, 1000, 3000) == ["S1", "S2"]  # touching counts
    assert speakers_in_range(turns, 2500, 4999) == []
    assert speakers_in_range(turns, 0, 10_000) == ["S1", "S2", "S3"]


# --- Diarization serde --------------------------------------------------------


def make_diarization() -> Diarization:
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    return Diarization(
        available=True,
        reason="",
        engine="sherpa-onnx",
        engine_version="1.13.4",
        segmentation_model="pyannote-segmentation-3.0",
        embedding_model="wespeaker_en_voxceleb_resnet34_LM",
        requested_num_speakers=2,
        detected_num_speakers=2,
        threshold=0.5,
        speakers=speaker_roster(turns),
        turns=turns,
    )


def test_diarization_round_trip_with_compact_turn_triplets() -> None:
    diarization = make_diarization()
    payload = diarization.to_dict()
    assert payload["turns"] == [[0, 5000, "S1"], [5000, 8000, "S2"]]
    assert "speaker_names" not in payload  # None never serialized
    assert Diarization.from_dict(payload) == diarization


def test_diarization_serializes_speaker_names_when_present() -> None:
    diarization = make_diarization()
    diarization.speaker_names = {"S1": "Alice"}
    payload = diarization.to_dict()
    assert payload["speaker_names"] == {"S1": "Alice"}
    assert Diarization.from_dict(payload).speaker_names == {"S1": "Alice"}


def test_diarization_from_dict_ignores_unknown_and_malformed() -> None:
    payload = make_diarization().to_dict()
    payload["embedding_dim"] = 256  # field from a future version
    payload["speakers"][0]["confidence"] = 0.9
    payload["turns"].append([1, 2])  # malformed triplet is skipped
    rebuilt = Diarization.from_dict(payload)
    assert rebuilt.detected_num_speakers == 2
    assert len(rebuilt.turns) == 2
    assert rebuilt.speakers[0].label == "S1"


# --- env ------------------------------------------------------------------


def test_diarize_default_off_and_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    assert diarize_default() is False
    for value in ("on", "1", "true", " ON "):
        monkeypatch.setenv("TALKTHROUGH_DIARIZE", value)
        assert diarize_default() is True
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "off")
    assert diarize_default() is False


def test_threshold_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_THRESHOLD", raising=False)
    assert clustering_threshold() == DEFAULT_THRESHOLD
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THRESHOLD", "0.72")
    assert clustering_threshold() == 0.72
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THRESHOLD", "not-a-float")
    assert clustering_threshold() == DEFAULT_THRESHOLD


def test_threads_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_THREADS", raising=False)
    default = diarization_threads()
    assert 1 <= default <= 4
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THREADS", "2")
    assert diarization_threads() == 2
    for bad in ("0", "-3", "many"):
        monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THREADS", bad)
        assert diarization_threads() == default
