"""process_media summary: frame-sampling honesty note (adaptive floor > 1 s)."""

from __future__ import annotations

from dataclasses import replace

from tests.conftest import make_manifest

from talkthrough_mcp.core.pipeline import ProcessResult, summarize


def _summary(*, duration_s: float, kind: str = "video") -> dict:
    manifest = make_manifest(kind=kind)
    manifest.media = replace(manifest.media, duration_s=duration_s)
    return summarize(ProcessResult(manifest=manifest, reused=False, elapsed_s=1.0))


def test_long_recording_summary_names_the_sampling_interval() -> None:
    # 73 min at the default 600-frame budget → floor ≈ 7.3 s
    frames = _summary(duration_s=4380.0)["frames"]
    assert frames["sampling_interval_s"] == 7
    assert "every ~7s" in frames["note"]
    assert "extract_frame" in frames["note"]


def test_short_recording_summary_stays_byte_stable() -> None:
    frames = _summary(duration_s=18.0)["frames"]
    assert frames == {"count": 4, "unique_count": 3, "cap_hit": False}


def test_audio_only_summary_never_carries_the_frame_note() -> None:
    frames = _summary(duration_s=4380.0, kind="audio")["frames"]
    assert "sampling_interval_s" not in frames
    assert "note" not in frames


def test_vocabulary_echo_count_appears_only_when_segments_were_dropped() -> None:
    manifest = make_manifest()
    plain = summarize(ProcessResult(manifest=manifest, reused=False, elapsed_s=1.0))
    assert "vocabulary_echo_trimmed" not in plain["transcript"]
    trimmed = summarize(
        ProcessResult(
            manifest=manifest, reused=False, elapsed_s=1.0, vocabulary_echo_trimmed=2
        )
    )
    assert trimmed["transcript"]["vocabulary_echo_trimmed"] == 2
