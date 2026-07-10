"""Wall-clock resolver: anchor video-relative timestamps to real time.

Priority ladder (§ DESIGN.md):

1. ``recorded_at`` tool param (user/agent override)      -> confidence ``exact``
2. QuickTime tag ``com.apple.quicktime.creationdate``    -> ``high``
   (macOS Cmd+Shift+5 writes it, WITH the local tz offset)
3. Container ``creation_time`` tag (usually UTC, no tz)  -> ``medium``
4. File mtime minus duration (recorders finalize the
   file at recording END)                                -> ``low``
5. Nothing                                               -> wall_clock = None

Every timestamped tool output exposes both ``t_ms`` (video-relative) and
``t_wall`` (ISO 8601) when the wall clock is known, so agents can correlate
a spoken remark with server/app logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

# Encoders sometimes stamp epoch-zero-ish garbage; treat anything this old as absent.
_MIN_PLAUSIBLE_YEAR = 1972

SOURCE_OVERRIDE = "override"
SOURCE_QUICKTIME = "quicktime"
SOURCE_METADATA = "metadata"
SOURCE_MTIME = "mtime"

_QUICKTIME_TAG = "com.apple.quicktime.creationdate"
_METADATA_TAG = "creation_time"


@dataclass(frozen=True)
class WallClock:
    start_utc: datetime  # tz-aware, UTC
    tz_offset_min: int | None  # recording-local offset when known
    source: str
    confidence: str

    def t_wall_iso(self, t_ms: int) -> str:
        """ISO 8601 wall time for a video-relative millisecond offset.

        Rendered in the recording-local offset when known (log correlation
        reads naturally), else in UTC.
        """
        instant = self.start_utc + timedelta(milliseconds=t_ms)
        if self.tz_offset_min is not None:
            instant = instant.astimezone(timezone(timedelta(minutes=self.tz_offset_min)))
        return instant.isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_utc": self.start_utc.isoformat(timespec="seconds"),
            "tz_offset_min": self.tz_offset_min,
            "source": self.source,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any] | None) -> WallClock | None:
        if not payload:
            return None
        start = datetime.fromisoformat(str(payload["start_utc"]))
        if start.tzinfo is None:  # defensive: manifests always store tz-aware
            start = start.replace(tzinfo=UTC)
        offset = payload.get("tz_offset_min")
        return WallClock(
            start_utc=start.astimezone(UTC),
            tz_offset_min=int(offset) if offset is not None else None,
            source=str(payload["source"]),
            confidence=str(payload["confidence"]),
        )


def _parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year < _MIN_PLAUSIBLE_YEAR:
        return None
    return parsed


def _offset_minutes(parsed: datetime) -> int | None:
    offset = parsed.utcoffset()
    if offset is None:
        return None
    return int(offset.total_seconds() // 60)


def _local_offset_minutes(at_epoch: float) -> int | None:
    local = datetime.fromtimestamp(at_epoch).astimezone()
    return _offset_minutes(local)


def resolve_wall_clock(
    *,
    recorded_at: str | None,
    format_tags: dict[str, str],
    mtime_epoch: float | None,
    duration_s: float,
) -> WallClock | None:
    """Walk the priority ladder; the first rung that parses wins."""
    if recorded_at:
        parsed = _parse_iso(recorded_at)
        if parsed is None:
            from .errors import ValidationError

            raise ValidationError(
                f"recorded_at is not ISO 8601: {recorded_at!r} "
                "(expected e.g. 2026-07-10T12:34:56+02:00)"
            )
        if parsed.tzinfo is None:
            # A human-noted recording time is naturally local time.
            parsed = parsed.astimezone()
        return WallClock(
            start_utc=parsed.astimezone(UTC),
            tz_offset_min=_offset_minutes(parsed),
            source=SOURCE_OVERRIDE,
            confidence="exact",
        )

    tags = {key.lower(): value for key, value in format_tags.items()}

    quicktime_raw = tags.get(_QUICKTIME_TAG)
    if quicktime_raw:
        parsed = _parse_iso(quicktime_raw)
        if parsed is not None and parsed.tzinfo is not None:
            return WallClock(
                start_utc=parsed.astimezone(UTC),
                tz_offset_min=_offset_minutes(parsed),
                source=SOURCE_QUICKTIME,
                confidence="high",
            )

    creation_raw = tags.get(_METADATA_TAG)
    if creation_raw:
        parsed = _parse_iso(creation_raw)
        if parsed is not None:
            if parsed.tzinfo is None:  # container creation_time is UTC by convention
                parsed = parsed.replace(tzinfo=UTC)
            return WallClock(
                start_utc=parsed.astimezone(UTC),
                tz_offset_min=None,
                source=SOURCE_METADATA,
                confidence="medium",
            )

    if mtime_epoch is not None and mtime_epoch > 0:
        end = datetime.fromtimestamp(mtime_epoch, tz=UTC)
        return WallClock(
            start_utc=end - timedelta(seconds=duration_s),
            tz_offset_min=_local_offset_minutes(mtime_epoch),
            source=SOURCE_MTIME,
            confidence="low",
        )

    return None
