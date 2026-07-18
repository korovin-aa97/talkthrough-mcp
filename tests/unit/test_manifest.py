"""Manifest round-trip, SRT formatting, slicing, frame queries, and search."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import make_manifest

from talkthrough_mcp.core.diarize import (
    Diarization,
    Turn,
    attribute_segments,
    speaker_roster,
)
from talkthrough_mcp.core.frames import Frame
from talkthrough_mcp.core.manifest import (
    FrameIndex,
    Manifest,
    format_srt,
    format_text,
    frame_validity_ms,
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


# --- diarization (additive schema) -------------------------------------------


def _diarized_manifest() -> Manifest:
    manifest = make_manifest(wall_clock=CLOCK)
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.segments = attribute_segments(manifest.transcript.segments, turns)
    manifest.transcript.diarization = Diarization(
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
    return manifest


def test_non_diarized_manifest_serializes_exactly_like_v01x() -> None:
    payload = make_manifest().to_dict()
    assert "diarization" not in payload["transcript"]
    assert all("speaker" not in segment for segment in payload["transcript"]["segments"])


def test_diarized_round_trip(tmp_path: Path) -> None:
    manifest = _diarized_manifest()
    save_manifest(manifest, tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded == manifest
    assert [s.speaker for s in loaded.transcript.segments] == ["S1", "S1", "S2"]
    diarization = loaded.transcript.diarization
    assert diarization is not None
    assert diarization.turns == [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    assert [stat.label for stat in diarization.speakers] == ["S1", "S2"]


def test_srt_prefixes_every_diarized_cue() -> None:
    manifest = _diarized_manifest()
    srt = format_srt(manifest.transcript.segments)
    assert "S1: This is the login page." in srt
    assert "S2: Settings look fine." in srt
    plain = format_srt(make_manifest().transcript.segments)
    assert "S1:" not in plain  # non-diarized output byte-stable


def test_format_text_prefixes_only_speaker_changes() -> None:
    manifest = _diarized_manifest()
    text = format_text(manifest.transcript.segments)
    assert text == (
        "S1: This is the login page. The dashboard shows an error message. "
        "S2: Settings look fine."
    )
    assert format_text(make_manifest().transcript.segments) == (
        "This is the login page. The dashboard shows an error message. Settings look fine."
    )


def test_search_hits_carry_speaker_on_diarized_jobs() -> None:
    manifest = _diarized_manifest()
    (hit,) = [h for h in search_manifest(manifest, "login") if h.source == "transcript"]
    assert hit.speaker == "S1"
    (plain,) = [
        h for h in search_manifest(make_manifest(), "login") if h.source == "transcript"
    ]
    assert plain.speaker is None


def test_from_dict_ignores_unknown_keys_from_newer_versions() -> None:
    payload = _diarized_manifest().to_dict()
    payload["transcript"]["segments"][0]["confidence"] = 0.93
    payload["transcript"]["word_stats"] = {"total": 40}
    payload["frames"]["items"][0]["blurhash"] = "LEHV6nWB2yk8"
    payload["caps"]["max_speakers"] = 16
    rebuilt = Manifest.from_dict(payload)
    assert rebuilt.transcript.segments[0].speaker == "S1"
    assert rebuilt.caps.max_seconds == 7200


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


# --- validity spans (#14) ----------------------------------------------------


def test_validity_span_runs_to_the_next_unique_frame() -> None:
    manifest = make_manifest()  # uniques at 0, 6006, 12012; duplicate at 1000
    frame_at_0, frame_at_6006 = manifest.frames.items[0], manifest.frames.items[2]
    assert frame_validity_ms(manifest, frame_at_0) == (0, 6006)
    assert frame_validity_ms(manifest, frame_at_6006) == (6006, 12012)


def test_validity_span_of_a_duplicate_is_its_unique_frames_span() -> None:
    manifest = _long_static_manifest()  # unique@1000, dups 2000-10000, unique@11000
    for frame in manifest.frames.items:
        if frame.duplicate_of == 1000:
            assert frame_validity_ms(manifest, frame) == (1000, 11000)
    unique = manifest.frames.items[0]
    assert frame_validity_ms(manifest, unique) == (1000, 11000)


def test_validity_span_of_the_last_frame_reaches_the_end_of_the_recording() -> None:
    manifest = make_manifest()  # duration 18.0 s, cap_hit False
    last = manifest.frames.items[-1]
    assert frame_validity_ms(manifest, last) == (12012, 18000)


def test_validity_span_of_a_single_frame_job_covers_the_whole_recording() -> None:
    manifest = make_manifest()
    only = Frame(ms=0, file="t00000000.jpg", duplicate_of=None)
    manifest.frames = FrameIndex(count=1, unique_count=1, cap_hit=False, items=[only])
    assert frame_validity_ms(manifest, only) == (0, 18000)


def test_validity_span_on_cap_hit_ends_at_the_last_sample_plus_step_not_media_end() -> None:
    """Issue #14 honesty rule: extraction stopped early ⇒ no claim past the
    last extracted sample (+ one sampling step); media end would overclaim."""
    manifest = make_manifest()
    frames = [Frame(ms=1000, file="t00001000.jpg", duplicate_of=None)]
    frames += [
        Frame(ms=ms, file=f"t{ms:08d}.jpg", duplicate_of=1000)
        for ms in range(2000, 11000, 1000)
    ]
    manifest.frames = FrameIndex(count=len(frames), unique_count=1, cap_hit=True, items=frames)
    # floor for 18 s / 600 frames stays 1 s → last sample 10000 + 1000 step
    assert frame_validity_ms(manifest, frames[0]) == (1000, 11000)
    assert manifest.media.duration_s * 1000 > 11000  # strictly before media end


def test_validity_span_before_a_cap_hit_tail_is_unaffected() -> None:
    manifest = _long_static_manifest()
    manifest.frames.cap_hit = True
    first_unique = manifest.frames.items[0]
    assert frame_validity_ms(manifest, first_unique) == (1000, 11000)
    last_unique = manifest.frames.items[-1]  # also the last extracted sample
    assert frame_validity_ms(manifest, last_unique) == (11000, 12000)


def test_validity_span_is_none_for_audio_only_jobs() -> None:
    manifest = make_manifest(kind="audio")
    assert frame_validity_ms(manifest, Frame(ms=0, file="t00000000.jpg")) is None
