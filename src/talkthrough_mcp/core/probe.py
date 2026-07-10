"""ffprobe wrapper: media validation input and metadata capture."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError
from .ffmpeg import ffprobe_path, run_tool


@dataclass(frozen=True)
class MediaInfo:
    path: str
    filename: str
    size_bytes: int
    duration_s: float
    has_video: bool
    has_audio: bool
    width: int
    height: int
    video_codec: str
    mtime_epoch: float
    format_tags: dict[str, str] = field(default_factory=dict)


def probe_media(media: Path) -> MediaInfo:
    """ffprobe the file and capture stream layout + container tags."""
    proc = run_tool(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media),
        ],
        timeout=120,
    )
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format") or {}
    try:
        duration_s = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration_s = 0.0
    if video is None and audio is None:
        raise ValidationError(f"no audio or video streams found in {media.name!r}")
    tags = {str(k): str(v) for k, v in (fmt.get("tags") or {}).items()}
    stat = media.stat()
    return MediaInfo(
        path=str(media),
        filename=media.name,
        size_bytes=stat.st_size,
        duration_s=duration_s,
        has_video=video is not None,
        has_audio=audio is not None,
        width=int(video.get("width") or 0) if video else 0,
        height=int(video.get("height") or 0) if video else 0,
        video_codec=str(video.get("codec_name") or "") if video else "",
        mtime_epoch=stat.st_mtime,
        format_tags=tags,
    )
