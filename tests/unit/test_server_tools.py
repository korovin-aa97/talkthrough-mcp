"""Server-layer payload contracts: search(speaker=) honesty + media_kind.

These drive the real tool functions against a manifest saved into an
isolated store — the payload exactly as a client receives it, no MCP
transport needed.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_manifest

from talkthrough_mcp.core.diarize import Diarization, Turn, attribute_segments, speaker_roster
from talkthrough_mcp.core.jobs import job_dir
from talkthrough_mcp.core.manifest import Manifest, save_manifest


def _store(manifest: Manifest) -> str:
    directory = job_dir(manifest.job_id)
    directory.mkdir(parents=True, exist_ok=True)
    save_manifest(manifest, directory)
    return manifest.job_id


def _diarize(manifest: Manifest) -> Manifest:
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.segments = attribute_segments(manifest.transcript.segments, turns)
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        engine="sherpa-onnx",
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=turns,
    )
    return manifest


# --- search(speaker=) on the wire --------------------------------------------


def test_search_speaker_filters_and_notes_ocr_exclusion(isolated_home: Path) -> None:
    from talkthrough_mcp.server import search

    job_id = _store(_diarize(make_manifest()))
    payload = search(job_id, "dashboard", speaker="s1")  # lowercase label normalized
    assert payload["speaker"] == "S1"
    assert payload["hit_count"] == 1
    assert [hit["source"] for hit in payload["hits"]] == ["transcript"]
    assert all(hit["speaker"] == "S1" for hit in payload["hits"])
    assert "ocr hits are excluded" in payload["note"]

    unfiltered = search(job_id, "dashboard")
    assert "note" not in unfiltered
    assert "speaker" not in {k for k in unfiltered if k != "hits"}
    assert {hit["source"] for hit in unfiltered["hits"]} == {"transcript", "ocr"}


def test_search_speaker_on_undiarized_job_is_honest_not_an_error(isolated_home: Path) -> None:
    from talkthrough_mcp.server import search

    job_id = _store(make_manifest())
    payload = search(job_id, "dashboard", speaker="S2")
    assert payload["hits"] == []
    assert payload["hit_count"] == 0
    assert payload["speaker"] == "S2"
    assert "not diarized" in payload["note"]
    assert "diarize=true" in payload["note"]


def test_search_blank_speaker_means_no_filter(isolated_home: Path) -> None:
    from talkthrough_mcp.server import search

    job_id = _store(make_manifest())
    payload = search(job_id, "dashboard", speaker="  ")
    assert payload["hit_count"] == 2
    assert "note" not in payload


# --- media_kind in get_transcript --------------------------------------------


def test_get_transcript_names_the_media_kind(isolated_home: Path) -> None:
    from talkthrough_mcp.server import get_transcript

    video_job = _store(make_manifest())
    assert get_transcript(video_job)["media_kind"] == "video"

    audio_job = _store(make_manifest(job_id="fedcba9876543210", kind="audio"))
    assert get_transcript(audio_job)["media_kind"] == "audio"


# --- v0.2.3 honesty contour: the escalation note reaches every entry point ----


def _dusty_threshold(manifest: Manifest) -> Manifest:
    """A threshold-mode over-detected job: 2 substantial voices + 26 dust
    clusters (the real A360 shape), no requested_num_speakers."""
    from talkthrough_mcp.core.diarize import SpeakerStat, Turn

    turns = [Turn(0, 40_000, "S1"), Turn(40_000, 75_000, "S2")]
    dust = [
        SpeakerStat(
            label=f"S{i + 3}", talk_time_ms=1_500, turn_count=1,
            first_ms=75_000, last_ms=76_500,
        )
        for i in range(26)
    ]
    manifest.transcript.segments = attribute_segments(manifest.transcript.segments, turns)
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        engine="sherpa-onnx",
        detected_num_speakers=28,
        threshold=0.5,
        speakers=speaker_roster(turns) + dust,
        turns=turns,
    )
    return manifest


def test_get_transcript_serves_the_escalation_note_byte_identical(
    isolated_home: Path,
) -> None:
    from talkthrough_mcp.core import pipeline
    from talkthrough_mcp.server import get_transcript

    job_id = _store(_dusty_threshold(make_manifest()))
    payload = get_transcript(job_id)
    manifest = _dusty_threshold(make_manifest())
    diarization = manifest.transcript.diarization
    assert diarization is not None
    expected = pipeline.threshold_escalation_note(diarization)
    assert expected is not None
    assert payload["diarization_note"] == expected
    assert payload["diarization_note"] == pipeline._summarize_diarization(diarization)["note"]


def test_get_transcript_has_no_note_on_clean_or_k_jobs(isolated_home: Path) -> None:
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.server import get_transcript

    # clean threshold job: every detected voice is substantial (>=30 s)
    clean_manifest = make_manifest()
    turns = [Turn(0, 40_000, "S1"), Turn(40_000, 75_000, "S2")]
    clean_manifest.transcript.segments = attribute_segments(
        clean_manifest.transcript.segments, turns
    )
    clean_manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        engine="sherpa-onnx",
        detected_num_speakers=2,
        threshold=0.5,
        speakers=speaker_roster(turns),
        turns=turns,
    )
    clean_job = _store(clean_manifest)
    assert "diarization_note" not in get_transcript(clean_job)

    k_manifest = _dusty_threshold(make_manifest(job_id="fedcba9876543210"))
    diarization = k_manifest.transcript.diarization
    assert diarization is not None
    diarization.requested_num_speakers = 28  # explicit k → the user already decided
    k_job = _store(k_manifest)
    assert "diarization_note" not in get_transcript(k_job)


def test_list_jobs_carries_the_30s_signal_only_on_overdetected_jobs(
    isolated_home: Path,
) -> None:
    from talkthrough_mcp.server import list_jobs

    threshold_job = _store(_dusty_threshold(make_manifest()))
    k_manifest = _diarize(make_manifest(job_id="fedcba9876543210"))
    diarization = k_manifest.transcript.diarization
    assert diarization is not None
    diarization.requested_num_speakers = 2
    k_job = _store(k_manifest)

    entries = {entry["job_id"]: entry for entry in list_jobs()["jobs"]}
    assert entries[threshold_job]["speakers"] == 28  # existing field, unchanged
    assert entries[threshold_job]["speakers_with_30s_plus"] == 2
    assert entries[k_job]["speakers"] == 2
    assert "speakers_with_30s_plus" not in entries[k_job]


# --- v0.2.3 search notes: unknown label + zero-hit multi-word ------------------


def test_search_unknown_speaker_label_names_the_roster(isolated_home: Path) -> None:
    from talkthrough_mcp.server import search

    job_id = _store(_diarize(make_manifest()))
    payload = search(job_id, "dashboard", speaker="s99")
    assert payload["hits"] == [] and payload["hit_count"] == 0
    assert payload["speaker"] == "S99"
    assert "not in this job's roster (S1-S2)" in payload["note"]
    assert "'S99'" in payload["note"]


def test_search_zero_hit_multiword_notes_explain_the_miss(isolated_home: Path) -> None:
    from talkthrough_mcp.server import search

    job_id = _store(make_manifest())
    # "dashboard settings" straddles segments 2|3 → the note names the spot
    straddle = search(job_id, "dashboard settings")
    assert straddle["hit_count"] == 0
    assert "t_ms=2500" in straddle["note"]
    assert "get_transcript" in straddle["note"]
    # "login settings" never meets even across adjacent segments → generic note
    generic = search(job_id, "login settings")
    assert generic["hit_count"] == 0
    assert "no single segment contains ALL the words" in generic["note"]
    # single-word zero hit: behavior unchanged — no note at all
    single = search(job_id, "flurble")
    assert single["hit_count"] == 0 and "note" not in single
    # non-zero-hit payloads stay note-free
    hit = search(job_id, "dashboard error")
    assert hit["hit_count"] >= 1 and "note" not in hit


def test_search_zero_hit_multiword_respects_the_speaker_filter(
    isolated_home: Path,
) -> None:
    """The straddle scan mirrors the filter: a pair split across two voices
    must not be offered to a single-voice query."""
    from talkthrough_mcp.server import search

    job_id = _store(_diarize(make_manifest()))
    payload = search(job_id, "dashboard settings", speaker="S1")
    assert payload["hit_count"] == 0
    assert "ocr hits are excluded" in payload["note"]  # 0.2.2 note survives, joined
    assert "no single segment contains ALL the words" in payload["note"]
    assert "t_ms=" not in payload["note"]  # segments 2|3 are S1|S2 — not S1's pair
