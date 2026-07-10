"""Manifest round-trip, SRT formatting, slicing, frame queries, and search."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import make_manifest

from talkthrough_mcp.core.frames import Frame
from talkthrough_mcp.core.manifest import (
    FrameIndex,
    Manifest,
    format_srt,
    frames_in_range,
    load_manifest,
    nearest_frame_ms,
    nearest_frames,
    representative_frame,
    save_manifest,
    search_manifest,
    slice_segments,
)
from talkthrough_mcp.core.wallclock import WallClock

CLOCK = WallClock(
    start_utc=datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC),
    tz_offset_min=None,
    source="metadata",
    confidence="medium",
)


def test_save_load_round_trip(tmp_path: Path) -> None:
    manifest = make_manifest(wall_clock=CLOCK)
    save_manifest(manifest, tmp_path)
    assert load_manifest(tmp_path) == manifest


def test_round_trip_without_wall_clock(tmp_path: Path) -> None:
    manifest = make_manifest(wall_clock=None)
    save_manifest(manifest, tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded == manifest
    assert loaded.t_wall_iso(1000) is None


def test_format_srt_known_output() -> None:
    manifest = make_manifest()
    srt = format_srt(manifest.transcript.segments[:2])
    assert srt == (
        "1\n00:00:00,000 --> 00:00:02,000\nThis is the login page.\n"
        "\n"
        "2\n00:00:02,500 --> 00:00:05,000\nThe dashboard shows an error message.\n"
    )


def test_srt_timestamps_cover_hours() -> None:
    manifest = make_manifest()
    segment = manifest.transcript.segments[0]
    shifted = type(segment)(seq=1, t0_ms=3_661_234, t1_ms=3_662_000, text="late remark")
    assert "01:01:01,234 --> 01:01:02,000" in format_srt([shifted])


def test_slice_segments_overlap_semantics() -> None:
    segments = make_manifest().transcript.segments
    assert [s.seq for s in slice_segments(segments, None, None)] == [1, 2, 3]
    assert [s.seq for s in slice_segments(segments, 2500, 6000)] == [2, 3]
    assert [s.seq for s in slice_segments(segments, 0, 1000)] == [1]
    assert slice_segments(segments, 9000, 10000) == []


def test_nearest_frames_serves_unique_only_by_default() -> None:
    manifest = make_manifest(wall_clock=CLOCK)
    picked = nearest_frames(manifest, at_ms=5900, count=2)
    assert [frame.ms for frame in picked] == [0, 6006]  # time order, duplicates skipped
    with_dups = nearest_frames(manifest, at_ms=1100, count=2, include_duplicates=True)
    assert [frame.ms for frame in with_dups] == [0, 1000]


def test_frames_in_range_thins_evenly() -> None:
    manifest = make_manifest()
    picked = frames_in_range(manifest, 0, 13_000, max_count=2)
    assert [frame.ms for frame in picked] == [0, 12012]
    everything = frames_in_range(manifest, 0, 13_000, max_count=10)
    assert [frame.ms for frame in everything] == [0, 6006, 12012]


def test_search_hits_transcript_and_ocr_with_wall_clock() -> None:
    manifest = make_manifest(wall_clock=CLOCK)
    hits = search_manifest(manifest, "DASHBOARD")
    assert [hit.source for hit in hits] == ["transcript", "ocr"]
    transcript_hit, ocr_hit = hits
    assert transcript_hit.t_ms == 2500
    assert transcript_hit.t_wall == "2026-07-10T10:00:02+00:00"
    assert transcript_hit.nearest_frame_ms == 0
    assert ocr_hit.frame_ms == 6006
    assert ocr_hit.t_wall == "2026-07-10T10:00:06+00:00"


def test_search_empty_query_returns_nothing() -> None:
    assert search_manifest(make_manifest(), "   ") == []


def test_from_dict_tolerates_extra_free_form_versions(tmp_path: Path) -> None:
    manifest = make_manifest()
    payload = manifest.to_dict()
    payload["tool_versions"]["ffmpeg"] = "ffmpeg version 7.0"
    rebuilt = Manifest.from_dict(payload)
    assert rebuilt.tool_versions["ffmpeg"] == "ffmpeg version 7.0"


def _long_static_manifest() -> Manifest:
    """One keyframe, a long deduplicated stretch, then a scene change."""
    manifest = make_manifest(wall_clock=CLOCK)
    frames = [Frame(ms=1000, file="t00001000.jpg", duplicate_of=None, ocr_text="STATE A")]
    frames += [
        Frame(ms=ms, file=f"t{ms:08d}.jpg", duplicate_of=1000, ocr_text=None)
        for ms in range(2000, 11000, 1000)
    ]
    frames.append(Frame(ms=11000, file="t00011000.jpg", duplicate_of=None, ocr_text="STATE B"))
    manifest.frames = FrameIndex(count=len(frames), unique_count=2, cap_hit=False, items=frames)
    return manifest


def test_representative_frame_resolves_duplicates_instead_of_jumping_scenes() -> None:
    manifest = _long_static_manifest()
    # At 9.5s the time-nearest UNIQUE frame is the next scene (11s), but the
    # screen still showed the 1s state — frame@9000 is duplicate_of=1000.
    assert nearest_frames(manifest, 9500, 1)[0].ms == 11000  # the old, misleading pick
    representative = representative_frame(manifest, 9500)
    assert representative is not None
    assert representative.ms == 1000
    assert nearest_frame_ms(manifest, 9500) == 1000  # search hits use this


def test_representative_frame_keeps_true_nearest_at_scene_boundary() -> None:
    manifest = _long_static_manifest()
    representative = representative_frame(manifest, 10800)
    assert representative is not None
    assert representative.ms == 11000


def test_representative_frame_none_for_audio_only() -> None:
    manifest = make_manifest(kind="audio")
    assert representative_frame(manifest, 1000) is None
    assert nearest_frame_ms(manifest, 1000) is None
