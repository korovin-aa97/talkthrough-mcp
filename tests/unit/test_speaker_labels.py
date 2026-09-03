"""Pure validation and patch semantics for durable speaker names."""

from __future__ import annotations

from typing import Any, cast

import pytest

from talkthrough_mcp.core.diarize import (
    Diarization,
    PendingSpeakerReviewContext,
    SpeakerStat,
)
from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.speaker_labels import apply_speaker_label_patch


def _diarization() -> Diarization:
    return Diarization(
        available=True,
        reason="",
        speakers=[
            SpeakerStat("S1", 5_000, 1, 0, 5_000),
            SpeakerStat("S2", 3_000, 1, 5_000, 8_000),
        ],
    )


def test_patch_trims_names_and_evidence_and_is_idempotent() -> None:
    diarization = _diarization()
    for _ in range(2):
        apply_speaker_label_patch(
            diarization,
            {" s1 ": "  Zoë  "},
            {"S1": "  introduced herself at 1200 ms  "},
        )
    assert diarization.speaker_names == {"S1": "Zoë"}
    assert diarization.speaker_name_evidence == {"S1": "introduced herself at 1200 ms"}


def test_duplicate_names_are_allowed_and_evidence_can_be_updated_separately() -> None:
    diarization = _diarization()
    apply_speaker_label_patch(diarization, {"S1": "Alex", "S2": "Alex"})
    apply_speaker_label_patch(diarization, {}, {"S2": "name plate at frame 6006"})
    assert diarization.speaker_names == {"S1": "Alex", "S2": "Alex"}
    assert diarization.speaker_name_evidence == {"S2": "name plate at frame 6006"}


@pytest.mark.parametrize("removed", [None, "", "   "])
def test_null_or_blank_name_removes_name_and_evidence(removed: str | None) -> None:
    diarization = _diarization()
    apply_speaker_label_patch(diarization, {"S1": "Vera"}, {"S1": "intro"})
    apply_speaker_label_patch(diarization, {"S1": removed}, {"S1": "ignored"})
    assert diarization.speaker_names is None
    assert diarization.speaker_name_evidence is None


def test_blank_evidence_removes_only_evidence() -> None:
    diarization = _diarization()
    apply_speaker_label_patch(diarization, {"S1": "Vera"}, {"S1": "intro"})
    apply_speaker_label_patch(diarization, {}, {"S1": "  "})
    assert diarization.speaker_names == {"S1": "Vera"}
    assert diarization.speaker_name_evidence is None


@pytest.mark.parametrize(
    ("labels", "evidence", "message"),
    [
        ({"S9": "Vera"}, None, "valid labels: S1, S2"),
        ({"S1": "x" * 101}, None, "exceeds 100"),
        ({"S1": "Vera\nAdmin"}, None, "control characters"),
        ({"S1": "Vera"}, {"S1": "x" * 501}, "exceeds 500"),
        ({"S1": "Vera"}, {"S1": "proof\tmaybe"}, "control characters"),
        ({}, {"S1": "intro"}, "requires that label in labels"),
    ],
)
def test_invalid_patches_fail_without_mutating(
    labels: dict[str, str | None], evidence: dict[str, str] | None, message: str
) -> None:
    diarization = _diarization()
    before = diarization.to_dict()
    with pytest.raises(ValidationError, match=message):
        apply_speaker_label_patch(diarization, labels, evidence)
    assert diarization.to_dict() == before


def test_duplicate_normalized_keys_and_non_string_values_are_rejected() -> None:
    diarization = _diarization()
    with pytest.raises(ValidationError, match="duplicate speaker label 'S1'"):
        apply_speaker_label_patch(diarization, {"S1": "A", " s1 ": "B"})
    bad = cast(dict[str, str | None], cast(Any, {"S1": 42}))
    with pytest.raises(ValidationError, match="must be a string or null"):
        apply_speaker_label_patch(diarization, bad)


def test_empty_final_mapping_serializes_as_absent_fields() -> None:
    diarization = _diarization()
    apply_speaker_label_patch(diarization, {"S1": "Vera"}, {"S1": "intro"})
    apply_speaker_label_patch(diarization, {"S1": None})
    payload = diarization.to_dict()
    assert "speaker_names" not in payload
    assert "speaker_name_evidence" not in payload


def test_explicit_label_patch_reviews_only_the_touched_pending_entries() -> None:
    diarization = _diarization()
    diarization.speaker_names_pending_review = {"S1": "Vera", "S2": "Tom"}
    diarization.speaker_name_evidence_pending_review = {
        "S1": "old intro",
        "S2": "old name plate",
    }
    diarization.speaker_names_pending_review_context = {
        "S1": PendingSpeakerReviewContext(longest_turn_at_ms=1000),
        "S2": PendingSpeakerReviewContext(longest_turn_at_ms=5000),
    }
    apply_speaker_label_patch(
        diarization,
        {"S1": "Vera"},
        {"S1": "re-verified against the current roster"},
    )
    assert diarization.speaker_names == {"S1": "Vera"}
    assert diarization.speaker_name_evidence == {
        "S1": "re-verified against the current roster"
    }
    assert diarization.speaker_names_pending_review == {"S2": "Tom"}
    assert diarization.speaker_name_evidence_pending_review == {"S2": "old name plate"}
    assert diarization.speaker_names_pending_review_context == {
        "S2": PendingSpeakerReviewContext(longest_turn_at_ms=5000)
    }

    apply_speaker_label_patch(diarization, {"S2": None})
    assert diarization.speaker_names_pending_review is None
    assert diarization.speaker_name_evidence_pending_review is None
    assert diarization.speaker_names_pending_review_context is None


def test_evidence_only_patch_does_not_claim_pending_identity_was_reviewed() -> None:
    diarization = _diarization()
    diarization.speaker_names = {"S1": "Vera"}
    diarization.speaker_names_pending_review = {"S2": "Tom"}
    apply_speaker_label_patch(diarization, {}, {"S1": "new proof"})
    assert diarization.speaker_names_pending_review == {"S2": "Tom"}


def test_stale_pending_label_can_only_be_removed_with_explicit_null() -> None:
    diarization = _diarization()
    diarization.speaker_names_pending_review = {"S3": "Ghost"}
    diarization.speaker_name_evidence_pending_review = {"S3": "old title card"}
    diarization.speaker_names_pending_review_context = {
        "S3": PendingSpeakerReviewContext(longest_turn_at_ms=9000)
    }

    before = diarization.to_dict()
    with pytest.raises(ValidationError, match=r"inactive.*cannot be confirmed"):
        apply_speaker_label_patch(diarization, {"S3": "Ghost"})
    assert diarization.to_dict() == before

    with pytest.raises(ValidationError, match=r"inactive.*cannot be confirmed"):
        apply_speaker_label_patch(diarization, {"S3": "   "})
    assert diarization.to_dict() == before

    with pytest.raises(ValidationError, match=r"evidence.*inactive"):
        apply_speaker_label_patch(diarization, {}, {"S3": "new proof"})
    assert diarization.to_dict() == before

    apply_speaker_label_patch(diarization, {" s3 ": None})
    assert diarization.speaker_names_pending_review is None
    assert diarization.speaker_name_evidence_pending_review is None
    assert diarization.speaker_names_pending_review_context is None


def test_unknown_label_error_names_current_and_pending_sets() -> None:
    diarization = _diarization()
    diarization.speaker_names_pending_review = {"S3": "Ghost"}
    with pytest.raises(
        ValidationError,
        match=r"current roster labels: S1, S2; pending-review labels: S3",
    ):
        apply_speaker_label_patch(diarization, {"S4": None})


def test_mixed_current_and_invalid_stale_patch_is_atomic() -> None:
    diarization = _diarization()
    diarization.speaker_names = {"S1": "Vera"}
    diarization.speaker_names_pending_review = {"S3": "Ghost"}
    before = diarization.to_dict()
    with pytest.raises(ValidationError, match=r"inactive.*cannot be confirmed"):
        apply_speaker_label_patch(
            diarization,
            {"S1": "Updated Vera", "S3": "Ghost"},
        )
    assert diarization.to_dict() == before
