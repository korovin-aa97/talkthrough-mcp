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
from . import audio, dedup, diarize, frames, jobs, ocr, stt
from .diarize import Diarization
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
    amended: bool = False  # diarization added to an existing job, whisper untouched


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


DIARIZE_STAGE = "identifying speakers (local)"
DIARIZE_PROGRESS_START = 0.56
DIARIZE_PROGRESS_END = 0.70


@dataclass(frozen=True)
class _DiarizeRequest:
    """The resolved diarization intent for one process_media call.

    ``explicit`` — the caller asked in the call itself (``diarize=true`` or a
    ``num_speakers``), as opposed to the TALKTHROUGH_DIARIZE env default.
    ``run`` — the stage should actually execute. ``engine_missing`` — the
    ambient default is on but the extra isn't installed (warn + degrade).
    """

    run: bool
    explicit: bool
    engine_missing: bool
    num_speakers: int | None


def _resolve_diarize_request(
    diarize_flag: bool | None, num_speakers: int | None
) -> _DiarizeRequest:
    """Validate diarization inputs against the degradation matrix.

    Explicit intent without the extra fails fast (BEFORE whisper spends
    minutes); the env-flipped default degrades with a warning, OCR-style.
    """
    explicit = diarize_flag is True or num_speakers is not None
    effective_on = diarize_flag if diarize_flag is not None else diarize.diarize_default()
    if num_speakers is not None:
        if num_speakers < 1:
            raise ValidationError(f"num_speakers must be >= 1, got {num_speakers}")
        if diarize_flag is False:
            raise ValidationError("num_speakers only makes sense with diarize=true")
        effective_on = True
    engine_missing = False
    if effective_on and not diarize.engine_available():
        if explicit:
            raise ValidationError(diarize.MISSING_EXTRA_REASON)
        logger.warning("TALKTHROUGH_DIARIZE=on ignored: %s", diarize.MISSING_EXTRA_REASON)
        effective_on = False
        engine_missing = True
    return _DiarizeRequest(
        run=effective_on,
        explicit=explicit,
        engine_missing=engine_missing,
        num_speakers=num_speakers,
    )


def _run_diarization(
    wav_path: Path,
    transcript: Transcript,
    request: _DiarizeRequest,
    report: ProgressFn,
) -> None:
    """Diarize the WAV and attach turns/roster/speakers to the transcript.

    Any engine failure (model download, native error) degrades to
    ``diarization.available=false`` with the reason — the transcript always
    survives. Only the fail-fast in ``_resolve_diarize_request`` raises.
    """
    report(DIARIZE_STAGE, DIARIZE_PROGRESS_START)
    try:
        diarizer = diarize.create_diarizer()
        if diarizer is None:
            transcript.diarization = Diarization(
                available=False, reason=diarize.MISSING_EXTRA_REASON
            )
            return

        def on_progress(fraction: float) -> None:
            span = DIARIZE_PROGRESS_END - DIARIZE_PROGRESS_START
            report(DIARIZE_STAGE, DIARIZE_PROGRESS_START + span * fraction)

        samples, sample_rate = diarize.load_wav_float32(wav_path)
        turns = diarizer.diarize(
            samples, sample_rate, num_speakers=request.num_speakers, on_progress=on_progress
        )
        transcript.segments = diarize.attribute_segments(transcript.segments, turns)
        transcript.diarization = Diarization(
            available=True,
            reason="",
            engine=diarizer.engine,
            engine_version=diarizer.engine_version,
            segmentation_model=diarizer.segmentation_model,
            embedding_model=diarizer.embedding_model,
            requested_num_speakers=request.num_speakers,
            detected_num_speakers=len({turn.speaker for turn in turns}),
            threshold=diarizer.threshold,
            speakers=diarize.speaker_roster(turns),
            turns=turns,
        )
    except Exception as exc:
        logger.warning("diarization failed, keeping the transcript without speakers: %s", exc)
        transcript.diarization = Diarization(available=False, reason=str(exc))


def _needs_diarize_amend(manifest: Manifest, request: _DiarizeRequest) -> bool:
    """Mirror of the explicit-model reuse rule: only EXPLICIT intent amends.

    A stored job without (working) diarization + an explicit request → run
    just the diarization stage. An explicit ``num_speakers`` differing from
    the stored one re-diarizes the same way. The env default deliberately
    never invalidates the store, and a diarized job served to a non-diarize
    call is a harmless superset.
    """
    if not (request.run and request.explicit and manifest.media.has_audio):
        return False
    stored = manifest.transcript.diarization
    if stored is None or not stored.available:
        return True
    return (
        request.num_speakers is not None
        and stored.requested_num_speakers != request.num_speakers
    )


def _amend_diarization(
    media: Path, manifest: Manifest, request: _DiarizeRequest, report: ProgressFn
) -> Manifest:
    """Re-extract the WAV and run ONLY diarization on an existing job.

    Whisper is not re-run; stored segments are re-attributed in place and the
    manifest is re-saved. ``created_at`` stays — the job identity is the same.
    """
    directory = jobs.job_dir(manifest.job_id)
    tool_timeout = max(600, int(manifest.media.duration_s * 4) + 120)
    wav_path = directory / "audio.wav"
    report("extracting audio", 0.10)
    try:
        audio.extract_wav(media, wav_path, timeout=tool_timeout)
        _run_diarization(wav_path, manifest.transcript, request, report)
    finally:
        wav_path.unlink(missing_ok=True)
    report("writing manifest", 0.99)
    save_manifest(manifest, directory)
    report("done", 1.0)
    return manifest


def process_media(
    path: str,
    *,
    recorded_at: str | None = None,
    vocabulary: str | None = None,
    language: str | None = None,
    model: str | None = None,
    diarize_speakers: bool | None = None,
    num_speakers: int | None = None,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> ProcessResult:
    """Run the full pipeline; instantly returns the existing manifest when reprocessing.

    ``diarize_speakers`` is tri-state: None follows the TALKTHROUGH_DIARIZE
    env default, an explicit True/False wins over it (the tool layer exposes
    it as the ``diarize`` parameter; the core name avoids shadowing the
    ``diarize`` module).
    """
    started = time.monotonic()
    model_name = resolve_whisper_model(model)
    diarize_request = _resolve_diarize_request(diarize_speakers, num_speakers)

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

    def reusable() -> Manifest | None:
        """Reuse unless forced — or unless an EXPLICIT per-call model differs
        from the stored transcript's model (silently returning the old model's
        text would betray the caller's intent). A changed env default
        deliberately does NOT invalidate the store."""
        if force or not jobs.job_exists(job_id):
            return None
        manifest = jobs.load_job(job_id)
        if model is not None and manifest.transcript.model != model_name:
            logger.info(
                "job %s exists with model %s but %s was explicitly requested — reprocessing",
                job_id, manifest.transcript.model, model_name,
            )
            return None
        return manifest

    manifest_hit = reusable()
    if manifest_hit is not None and not _needs_diarize_amend(manifest_hit, diarize_request):
        logger.info("job %s already processed — returning existing manifest", job_id)
        return ProcessResult(
            manifest=manifest_hit, reused=True, elapsed_s=time.monotonic() - started
        )

    with jobs.job_lock(job_id):
        # Re-check under the lock: a concurrent call may have just finished it.
        manifest_hit = reusable()
        if manifest_hit is not None:
            if _needs_diarize_amend(manifest_hit, diarize_request):
                logger.info(
                    "job %s exists — amending diarization only, whisper is not re-run", job_id
                )
                amended = _amend_diarization(media, manifest_hit, diarize_request, report)
                return ProcessResult(
                    manifest=amended,
                    reused=True,
                    elapsed_s=time.monotonic() - started,
                    amended=True,
                )
            return ProcessResult(
                manifest=manifest_hit, reused=True, elapsed_s=time.monotonic() - started
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
                # Diarization (when on) takes the 0.56→0.70 window; whisper
                # keeps its full 0.15→0.70 span otherwise.
                stt_span = (DIARIZE_PROGRESS_START - 0.15) if diarize_request.run else 0.55

                def on_segment(t1_ms: int) -> None:
                    report("transcribing (local whisper)", 0.15 + stt_span * (t1_ms / duration_ms))

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
                if diarize_request.run:
                    # The stage eats the same WAV — it must run before unlink.
                    _run_diarization(wav_path, transcript, diarize_request, report)
                elif diarize_request.engine_missing:
                    transcript.diarization = Diarization(
                        available=False, reason=diarize.MISSING_EXTRA_REASON
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
                duration_s=info.duration_s,
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


SUMMARY_ROSTER_CAP = 12
SUBSTANTIAL_TALK_MS = 30_000


def roster_payload(diarization: Diarization) -> tuple[list[dict[str, Any]], int]:
    """Top speakers by talk time, capped for the token budget.

    Threshold-mode clustering on real meetings produces dozens of sub-30 s
    micro-clusters; serving all of them in every response both floods the
    context and invites agents to read the cluster count as a headcount.
    Returns ``(entries, hidden_count)`` — entries stay in label order.
    """
    ranked = sorted(diarization.speakers, key=lambda s: -s.talk_time_ms)[:SUMMARY_ROSTER_CAP]
    kept = {stat.label for stat in ranked}
    entries = [
        {"label": stat.label, "talk_time_ms": stat.talk_time_ms, "turn_count": stat.turn_count}
        for stat in diarization.speakers
        if stat.label in kept
    ]
    return entries, len(diarization.speakers) - len(entries)


def _summarize_diarization(diarization: Diarization) -> dict[str, Any]:
    """Compact summary block: roster without first/last timestamps."""
    if not diarization.available:
        return {"available": False, "reason": diarization.reason}
    speakers, hidden = roster_payload(diarization)
    payload: dict[str, Any] = {
        "available": True,
        "detected_num_speakers": diarization.detected_num_speakers,
        "speakers": speakers,
    }
    if hidden:
        payload["speakers_truncated"] = hidden
    if diarization.requested_num_speakers is not None:
        payload["requested_num_speakers"] = diarization.requested_num_speakers
    else:
        substantial = sum(
            1 for s in diarization.speakers if s.talk_time_ms >= SUBSTANTIAL_TALK_MS
        )
        if substantial < (diarization.detected_num_speakers or 0):
            # threshold-mode honesty: clusters != people; give agents the
            # number worth reporting and the lever that fixes it
            payload["speakers_with_30s_plus"] = substantial
            payload["note"] = (
                "threshold clustering over-detects on real meetings — treat "
                "speakers_with_30s_plus as the likely headcount, or re-run "
                "with num_speakers for an exact roster"
            )
    return payload


def summarize(result: ProcessResult) -> dict[str, Any]:
    """Compact, context-friendly summary — never the full payload."""
    manifest = result.manifest
    segments = manifest.transcript.segments
    preview = [
        {
            "seq": segment.seq,
            "t_ms": segment.t0_ms,
            "t_wall": manifest.t_wall_iso(segment.t0_ms),
            **({"speaker": segment.speaker} if segment.speaker else {}),
            "text": segment.text,
        }
        for segment in segments[:TRANSCRIPT_PREVIEW_SEGMENTS]
    ]
    frames_with_text = sum(1 for frame in manifest.frames.items if frame.ocr_text)
    diarization = manifest.transcript.diarization
    return {
        "job_id": manifest.job_id,
        "reused": result.reused,
        **({"diarization_amended": True} if result.amended else {}),
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
        **({"diarization": _summarize_diarization(diarization)} if diarization else {}),
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
