"""Shared test helpers: isolated job store + manifest factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from talkthrough_mcp.core.frames import Frame
from talkthrough_mcp.core.manifest import (
    Caps,
    FrameIndex,
    Manifest,
    MediaMeta,
    Transcript,
)
from talkthrough_mcp.core.stt import SttSegment
from talkthrough_mcp.core.wallclock import WallClock


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the job store at a throwaway directory."""
    home = tmp_path / "talkthrough-home"
    monkeypatch.setenv("TALKTHROUGH_HOME", str(home))
    return home


def make_manifest(
    *,
    job_id: str = "abcdef0123456789",
    created_at: str = "2026-07-10T10:05:00+00:00",
    wall_clock: WallClock | None = None,
    kind: str = "video",
) -> Manifest:
    segments = [
        SttSegment(seq=1, t0_ms=0, t1_ms=2000, text="This is the login page."),
        SttSegment(seq=2, t0_ms=2500, t1_ms=5000, text="The dashboard shows an error message."),
        SttSegment(seq=3, t0_ms=6000, t1_ms=8000, text="Settings look fine."),
    ]
    frames = [
        Frame(ms=0, file="t00000000.jpg", duplicate_of=None, ocr_text="SCENE LOGIN PAGE"),
        Frame(ms=1000, file="t00001000.jpg", duplicate_of=0, ocr_text=None),
        Frame(ms=6006, file="t00006006.jpg", duplicate_of=None, ocr_text="SCENE DASHBOARD ERROR"),
        Frame(ms=12012, file="t00012012.jpg", duplicate_of=None, ocr_text="SCENE SETTINGS"),
    ]
    if kind == "audio":
        frames = []
    return Manifest(
        schema="talkthrough-manifest/v1",
        job_id=job_id,
        created_at=created_at,
        media=MediaMeta(
            path=f"/recordings/{job_id}.mp4",
            filename=f"{job_id}.mp4",
            kind=kind,
            duration_s=18.0,
            size_bytes=1_000_000,
            width=1280 if kind == "video" else 0,
            height=720 if kind == "video" else 0,
            video_codec="h264" if kind == "video" else "",
            has_audio=True,
            has_video=kind == "video",
        ),
        wall_clock=wall_clock,
        transcript=Transcript(
            available=True, reason="", language="en", model="tiny", segments=segments
        ),
        frames=FrameIndex(
            count=len(frames),
            unique_count=sum(1 for frame in frames if frame.is_unique),
            cap_hit=False,
            items=frames,
        ),
        caps=Caps(max_seconds=7200, max_frames=600, scene_threshold=0.10, ocr=True),
        tool_versions={"talkthrough-mcp": "0.1.0"},
    )
