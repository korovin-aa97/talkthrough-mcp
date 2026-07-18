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
