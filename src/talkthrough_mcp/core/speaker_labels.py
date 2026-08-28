"""Validation and deterministic patching for durable speaker names."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping

from .diarize import Diarization
from .errors import ValidationError

MAX_SPEAKER_NAME_CHARS = 100
MAX_SPEAKER_EVIDENCE_CHARS = 500


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _normalize_patch_keys(
    values: Mapping[str, object], valid_labels: list[str], field: str
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_label, value in values.items():
        label = raw_label.strip().upper()
        if label not in valid_labels:
            raise ValidationError(
                f"unknown speaker label {raw_label!r} in {field}; valid labels: "
                + ", ".join(valid_labels)
            )
        if label in normalized:
            raise ValidationError(
                f"duplicate speaker label {label!r} in {field}; valid labels: "
                + ", ".join(valid_labels)
            )
        normalized[label] = value
    return normalized


def apply_speaker_label_patch(
    diarization: Diarization,
    labels: Mapping[str, str | None],
    evidence: Mapping[str, str] | None = None,
) -> None:
    """Validate and atomically-in-memory apply one names/evidence patch."""
    valid_labels = [speaker.label for speaker in diarization.speakers]
    if not valid_labels:
        raise ValidationError("this diarized job has an empty speaker roster")
    label_patch = _normalize_patch_keys(labels, valid_labels, "labels")
    evidence_patch = _normalize_patch_keys(evidence or {}, valid_labels, "evidence")

    names = dict(diarization.speaker_names or {})
    saved_evidence = dict(diarization.speaker_name_evidence or {})
    validated_names: dict[str, str | None] = {}
    for label, raw_value in label_patch.items():
        if raw_value is not None and not isinstance(raw_value, str):
            raise ValidationError(f"speaker name for {label} must be a string or null")
        name = raw_value.strip() if isinstance(raw_value, str) else ""
        if len(name) > MAX_SPEAKER_NAME_CHARS:
            raise ValidationError(
                f"speaker name for {label} exceeds {MAX_SPEAKER_NAME_CHARS} characters; "
                f"valid labels: {', '.join(valid_labels)}"
            )
        if _has_control_characters(name):
            raise ValidationError(
                f"speaker name for {label} contains control characters; valid labels: "
                + ", ".join(valid_labels)
            )
        validated_names[label] = name or None

    validated_evidence: dict[str, str | None] = {}
    for label, raw_value in evidence_patch.items():
        if label not in label_patch and label not in names:
            raise ValidationError(
                f"evidence for {label} requires that label in labels or an already saved name"
            )
        if not isinstance(raw_value, str):
            raise ValidationError(f"speaker evidence for {label} must be a string")
        value = raw_value.strip()
        if len(value) > MAX_SPEAKER_EVIDENCE_CHARS:
            raise ValidationError(
                f"speaker evidence for {label} exceeds {MAX_SPEAKER_EVIDENCE_CHARS} characters; "
                f"valid labels: {', '.join(valid_labels)}"
            )
        if _has_control_characters(value):
            raise ValidationError(
                f"speaker evidence for {label} contains control characters; valid labels: "
                + ", ".join(valid_labels)
            )
        validated_evidence[label] = value or None

    for label, stored_name in validated_names.items():
        if stored_name is None:
            names.pop(label, None)
            saved_evidence.pop(label, None)
        else:
            names[label] = stored_name
    for label, evidence_value in validated_evidence.items():
        if label not in names:
            continue  # a same-call name deletion always removes its evidence
        if evidence_value is None:
            saved_evidence.pop(label, None)
        else:
            saved_evidence[label] = evidence_value

    saved_evidence = {label: value for label, value in saved_evidence.items() if label in names}
    diarization.speaker_names = names or None
    diarization.speaker_name_evidence = saved_evidence or None
