"""The deterministic processing pipeline: media file -> manifest.

No LLM anywhere — the calling agent brings the intelligence. Stages:

1. content hash -> job_id (idempotence check, instant on reprocess)
2. ffprobe validation + caps + disk preflight
3. wall-clock resolution (override > QuickTime tag > creation_time > mtime)
4. audio -> 16 kHz WAV -> faster-whisper timestamped segments
5. one-pass keyframe extraction (video only), scaled to <=1568 px
6. perceptual dedup (dHash), OCR of unique frames
7. manifest.json
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from . import audio, dedup, frames, jobs, ocr, stt
from .errors import ValidationError
from .ffmpeg import ffmpeg_version
from .manifest import (
    FRAMES_DIR_NAME,
    Caps,
    FrameIndex,
    Manifest,
    MediaMeta,
    Transcript,
    save_manifest,
)
from .probe import MediaInfo, probe_media
from .wallclock import resolve_wall_clock

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mov", ".mp4", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".flac"}

DEFAULT_MAX_SECONDS = 7200
DEFAULT_MAX_FRAMES = 600
DEFAULT_WHISPER_MODEL = "small"

# Names faster-whisper auto-downloads by alias (kept in sync with
# faster_whisper.utils._MODELS; validated here so a typo fails fast with a
# clear message instead of a Hugging Face 404 mid-pipeline).
ALLOWED_WHISPER_MODELS = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v1",
        "large-v2",
        "large-v3",
        "large",
        "large-v3-turbo",
        "turbo",
        "distil-small.en",
        "distil-medium.en",
        "distil-large-v2",
        "distil-large-v3",
        "distil-large-v3.5",
    }
)

TRANSCRIPT_PREVIEW_SEGMENTS = 15

ProgressFn = Callable[[str, float], None]


@dataclass(frozen=True)
class ProcessResult:
    manifest: Manifest
    reused: bool
    elapsed_s: float


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring non-integer %s=%r", name, raw)
        return default


def max_seconds_cap() -> int:
    return _env_int("TALKTHROUGH_MAX_SECONDS", DEFAULT_MAX_SECONDS)


def max_frames_cap() -> int:
    return _env_int("TALKTHROUGH_MAX_FRAMES", DEFAULT_MAX_FRAMES)


def whisper_model_name() -> str:
    return os.environ.get("TALKTHROUGH_WHISPER_MODEL", DEFAULT_WHISPER_MODEL).strip()


def resolve_whisper_model(override: str | None) -> str:
    """Per-call override > env default; unknown names fail fast with the allowlist."""
    name = (override or whisper_model_name()).strip()
    if name not in ALLOWED_WHISPER_MODELS:
        raise ValidationError(
            f"unknown whisper model {name!r} — allowed: {', '.join(sorted(ALLOWED_WHISPER_MODELS))}"
        )
    return name


def _validate_extension(media: Path) -> None:
    suffix = media.suffix.lower()
    if suffix not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS | AUDIO_EXTENSIONS))
        raise ValidationError(
            f"unsupported file type {suffix!r} for {media.name!r} — supported: {supported}"
        )


def _validate_caps(info: MediaInfo, out_root: Path) -> None:
    if info.duration_s <= 0:
        raise ValidationError(f"could not determine duration of {info.filename!r}")
    cap_seconds = max_seconds_cap()
    if info.duration_s > cap_seconds:
        raise ValidationError(
            f"duration {info.duration_s:.0f}s exceeds the {cap_seconds}s cap "
            "(override with TALKTHROUGH_MAX_SECONDS)"
        )
    free = shutil.disk_usage(str(out_root)).free
    if free < 2 * info.size_bytes:
        raise ValidationError(
            f"free disk {free} bytes < 2x media size {info.size_bytes} bytes — "
            "free up space and retry"
        )


def _tool_versions() -> dict[str, str]:
    import importlib.metadata

    versions = {
        "talkthrough-mcp": __version__,
        "ffmpeg": ffmpeg_version(),
    }
    for package in ("faster-whisper", "rapidocr"):
        with contextlib.suppress(Exception):  # version probing is best-effort
            versions[package] = importlib.metadata.version(package)
    return versions


def process_media(
    path: str,
    *,
    recorded_at: str | None = None,
    vocabulary: str | None = None,
    language: str | None = None,
    model: str | None = None,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> ProcessResult:
    """Run the full pipeline; instantly returns the existing manifest when reprocessing."""
    started = time.monotonic()
    model_name = resolve_whisper_model(model)

    def report(stage: str, fraction: float) -> None:
        if progress is not None:
            progress(stage, max(0.0, min(1.0, fraction)))

    media = Path(path).expanduser()
    if not media.is_file():
        raise ValidationError(f"file not found: {media}")
    media = media.resolve()
    _validate_extension(media)

    report("hashing file", 0.02)
    job_id = jobs.compute_job_id(media)

    if jobs.job_exists(job_id) and not force:
        manifest = jobs.load_job(job_id)
        logger.info("job %s already processed — returning existing manifest", job_id)
        return ProcessResult(
            manifest=manifest, reused=True, elapsed_s=time.monotonic() - started
        )

    with jobs.job_lock(job_id):
        # Re-check under the lock: a concurrent call may have just finished it.
        if jobs.job_exists(job_id) and not force:
            manifest = jobs.load_job(job_id)
            return ProcessResult(
                manifest=manifest, reused=True, elapsed_s=time.monotonic() - started
            )

        report("probing media", 0.05)
        info = probe_media(media)
        directory = jobs.job_dir(job_id)
        _validate_caps(info, directory)
        wall_clock = resolve_wall_clock(
            recorded_at=recorded_at,
            format_tags=info.format_tags,
            mtime_epoch=info.mtime_epoch,
            duration_s=info.duration_s,
        )

        frames_directory = directory / FRAMES_DIR_NAME
        if force:
            shutil.rmtree(frames_directory, ignore_errors=True)
            (directory / "manifest.json").unlink(missing_ok=True)

        tool_timeout = max(600, int(info.duration_s * 4) + 120)
        duration_ms = max(1, int(info.duration_s * 1000))

        transcript = Transcript(
            available=False, reason="no audio stream in recording", language=None, model=None
        )
        if info.has_audio:
            report("extracting audio", 0.10)
            wav_path = directory / "audio.wav"
            try:
                audio.extract_wav(media, wav_path, timeout=tool_timeout)
                report("transcribing (local whisper)", 0.15)

                def on_segment(t1_ms: int) -> None:
                    report("transcribing (local whisper)", 0.15 + 0.55 * (t1_ms / duration_ms))

                stt_result = stt.transcribe(
                    wav_path,
                    model_name=model_name,
                    language=language,
                    vocabulary=vocabulary,
                    on_segment=on_segment,
                )
                transcript = Transcript(
                    available=True,
                    reason="",
                    language=stt_result.language,
                    model=stt_result.model,
                    language_probability=stt_result.language_probability,
                    segments=list(stt_result.segments),
                )
            finally:
                wav_path.unlink(missing_ok=True)

        frame_index = FrameIndex(count=0, unique_count=0, cap_hit=False)
        ocr_ran = False
        if info.has_video:
            report("extracting keyframes", 0.72)
            extracted, cap_hit = frames.extract_keyframes(
                media,
                frames_directory,
                max_frames=max_frames_cap(),
                timeout=tool_timeout,
            )
            report("deduplicating frames", 0.82)
            dedup.mark_duplicates(extracted, frames_directory)
            unique = [frame for frame in extracted if frame.is_unique]

            engine = ocr.create_engine()
            if engine is not None:
                ocr_ran = True
                for index, frame in enumerate(unique):
                    fraction = 0.86 + 0.12 * (index / max(1, len(unique)))
                    report("reading text on frames (OCR)", fraction)
                    text = ocr.ocr_image(engine, frames_directory / frame.file)
                    frame.ocr_text = text or None

            frame_index = FrameIndex(
                count=len(extracted),
                unique_count=len(unique),
                cap_hit=cap_hit,
                items=extracted,
            )

        manifest = Manifest(
            schema="talkthrough-manifest/v1",
            job_id=job_id,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            media=MediaMeta(
                path=info.path,
                filename=info.filename,
                kind="video" if info.has_video else "audio",
                duration_s=info.duration_s,
                size_bytes=info.size_bytes,
                width=info.width,
                height=info.height,
                video_codec=info.video_codec,
                has_audio=info.has_audio,
                has_video=info.has_video,
            ),
            wall_clock=wall_clock,
            transcript=transcript,
            frames=frame_index,
            caps=Caps(
                max_seconds=max_seconds_cap(),
                max_frames=max_frames_cap(),
                scene_threshold=frames.DEFAULT_SCENE_THRESHOLD,
                ocr=ocr_ran,
            ),
            tool_versions=_tool_versions(),
        )
        report("writing manifest", 0.99)
        save_manifest(manifest, directory)

    report("done", 1.0)
    return ProcessResult(manifest=manifest, reused=False, elapsed_s=time.monotonic() - started)


def summarize(result: ProcessResult) -> dict[str, Any]:
    """Compact, context-friendly summary — never the full payload."""
    manifest = result.manifest
    segments = manifest.transcript.segments
    preview = [
        {
            "seq": segment.seq,
            "t_ms": segment.t0_ms,
            "t_wall": manifest.t_wall_iso(segment.t0_ms),
            "text": segment.text,
        }
        for segment in segments[:TRANSCRIPT_PREVIEW_SEGMENTS]
    ]
    frames_with_text = sum(1 for frame in manifest.frames.items if frame.ocr_text)
    return {
        "job_id": manifest.job_id,
        "reused": result.reused,
        "elapsed_s": round(result.elapsed_s, 2),
        "media": {
            "filename": manifest.media.filename,
            "kind": manifest.media.kind,
            "duration_s": manifest.media.duration_s,
            "width": manifest.media.width,
            "height": manifest.media.height,
        },
        "wall_clock": manifest.wall_clock.to_dict() if manifest.wall_clock else None,
        "transcript": {
            "available": manifest.transcript.available,
            "reason": manifest.transcript.reason or None,
            "language": manifest.transcript.language,
            "language_probability": manifest.transcript.language_probability,
            "model": manifest.transcript.model,
            "segment_count": len(segments),
            "preview_segments": preview,
            "preview_truncated": len(segments) > len(preview),
        },
        "frames": {
            "count": manifest.frames.count,
            "unique_count": manifest.frames.unique_count,
            "cap_hit": manifest.frames.cap_hit,
        },
        "ocr": {"enabled": manifest.caps.ocr, "unique_frames_with_text": frames_with_text},
        "next_steps": (
            "use get_transcript / get_moment / get_frames / search with this job_id; "
            "responses are paginated — nothing else is loaded until you ask"
        ),
    }
