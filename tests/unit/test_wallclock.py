"""Wall-clock resolver ladder: every rung, the fall-through order, and t_wall math."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.wallclock import (
    SOURCE_METADATA,
    SOURCE_MTIME,
    SOURCE_OVERRIDE,
    SOURCE_QUICKTIME,
    WallClock,
    resolve_wall_clock,
)

# Shapes captured from real ffprobe -show_format output.
QUICKTIME_TAGS = {
    "major_brand": "qt  ",
    "com.apple.quicktime.creationdate": "2026-07-10T12:34:56+0200",
    "creation_time": "2026-07-10T10:34:56.000000Z",
}
CONTAINER_TAGS = {
    "major_brand": "isom",
    "encoder": "Lavf61.7.100",
    "creation_time": "2026-07-10T10:00:00.000000Z",
}
EPOCH_GARBAGE_TAGS = {"creation_time": "1970-01-01T00:00:00.000000Z"}


def resolve(**overrides: object) -> WallClock | None:
    kwargs: dict = {
        "recorded_at": None,
        "format_tags": {},
        "mtime_epoch": None,
        "duration_s": 18.0,
    }
    kwargs.update(overrides)
    return resolve_wall_clock(**kwargs)


def test_override_wins_over_everything() -> None:
    clock = resolve(recorded_at="2026-07-10T12:00:00+02:00", format_tags=QUICKTIME_TAGS)
    assert clock is not None
    assert clock.source == SOURCE_OVERRIDE
    assert clock.confidence == "exact"
    assert clock.start_utc == datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC)
    assert clock.tz_offset_min == 120


def test_override_naive_is_interpreted_as_local_time() -> None:
    clock = resolve(recorded_at="2026-07-10T12:00:00")
    assert clock is not None
    expected = datetime(2026, 7, 10, 12, 0, 0).astimezone().astimezone(UTC)
    assert clock.start_utc == expected
    assert clock.source == SOURCE_OVERRIDE


def test_override_garbage_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="ISO 8601"):
        resolve(recorded_at="yesterday around noon")


def test_quicktime_tag_carries_tz_offset() -> None:
    clock = resolve(format_tags=QUICKTIME_TAGS)
    assert clock is not None
    assert clock.source == SOURCE_QUICKTIME
    assert clock.confidence == "high"
    assert clock.start_utc == datetime(2026, 7, 10, 10, 34, 56, tzinfo=UTC)
    assert clock.tz_offset_min == 120


def test_container_creation_time_is_utc_without_local_offset() -> None:
    clock = resolve(format_tags=CONTAINER_TAGS)
    assert clock is not None
    assert clock.source == SOURCE_METADATA
    assert clock.confidence == "medium"
    assert clock.start_utc == datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC)
    assert clock.tz_offset_min is None


def test_tag_keys_match_case_insensitively() -> None:
    clock = resolve(format_tags={"Creation_Time": "2026-07-10T10:00:00.000000Z"})
    assert clock is not None
    assert clock.source == SOURCE_METADATA


def test_epoch_garbage_creation_time_falls_through_to_mtime() -> None:
    mtime = datetime(2026, 7, 10, 11, 0, 18, tzinfo=UTC).timestamp()
    clock = resolve(format_tags=EPOCH_GARBAGE_TAGS, mtime_epoch=mtime, duration_s=18.0)
    assert clock is not None
    assert clock.source == SOURCE_MTIME


def test_mtime_is_recording_end_so_duration_is_subtracted() -> None:
    mtime = datetime(2026, 7, 10, 11, 0, 18, tzinfo=UTC).timestamp()
    clock = resolve(mtime_epoch=mtime, duration_s=18.0)
    assert clock is not None
    assert clock.source == SOURCE_MTIME
    assert clock.confidence == "low"
    assert clock.start_utc == datetime(2026, 7, 10, 11, 0, 0, tzinfo=UTC)
    local_offset = datetime.fromtimestamp(mtime).astimezone().utcoffset()
    assert local_offset is not None
    assert clock.tz_offset_min == int(local_offset.total_seconds() // 60)


def test_nothing_resolves_to_none() -> None:
    assert resolve() is None


def test_t_wall_iso_utc_rendering() -> None:
    clock = WallClock(
        start_utc=datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC),
        tz_offset_min=None,
        source=SOURCE_METADATA,
        confidence="medium",
    )
    assert clock.t_wall_iso(6000) == "2026-07-10T10:00:06+00:00"


def test_t_wall_iso_renders_in_recording_local_offset_when_known() -> None:
    clock = WallClock(
        start_utc=datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC),
        tz_offset_min=120,
        source=SOURCE_QUICKTIME,
        confidence="high",
    )
    assert clock.t_wall_iso(6000) == "2026-07-10T12:00:06+02:00"


def test_manifest_dict_round_trip() -> None:
    clock = resolve(format_tags=QUICKTIME_TAGS)
    assert clock is not None
    assert WallClock.from_dict(clock.to_dict()) == clock
    assert WallClock.from_dict(None) is None
