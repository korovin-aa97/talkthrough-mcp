"""Exception types shared across the core pipeline and the MCP tool layer.

Messages are user-facing: MCP tools surface them verbatim, so every message
must say what went wrong AND what to do instead.
"""

from __future__ import annotations


class TalkthroughError(Exception):
    """Base class for all expected talkthrough failures."""


class ValidationError(TalkthroughError):
    """Bad input: unsupported file, caps exceeded, missing path."""


class UnknownJobError(TalkthroughError):
    """The referenced job_id has no manifest in the job store."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"job not found: {job_id!r} — process the file with process_media first, "
            "or call list_jobs to see available job ids"
        )


class AudioOnlyJobError(TalkthroughError):
    """Frame retrieval was attempted on a job without a video stream."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"audio-only job: {job_id!r} has a transcript but no frames — "
            "use get_transcript, get_moment or search instead of frame tools"
        )


class ToolFailureError(TalkthroughError):
    """An external tool (ffmpeg/ffprobe/whisper/ocr) failed unexpectedly."""
