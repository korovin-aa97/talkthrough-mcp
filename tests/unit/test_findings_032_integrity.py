"""0.3.2 external findings F1/F2/F8: damaged manifests, interrupted rebuild
commits, and the disk preflight of a forced rebuild — plus the response notes
that make each state visible instead of silent."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.conftest import make_manifest
from tests.unit.test_pipeline_config import (
    _stored_force_identity_job,
    _stub_successful_force,
)

from talkthrough_mcp.core import jobs, pipeline
from talkthrough_mcp.core.errors import ToolFailureError, ValidationError
from talkthrough_mcp.core.manifest import Manifest, save_manifest

# Recent stamps: gc's age pass (30 days) must never be what removes a job here.
OLD = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
NEW = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")


def _materialize(
    job_id: str,
    directory: Path,
    *,
    marker: bytes,
    created_at: str,
    keep_frames: int | None = None,
    media_path: str | None = None,
) -> Manifest:
    manifest = make_manifest(job_id=job_id, created_at=created_at)
    if keep_frames is not None:
        manifest.frames.items = manifest.frames.items[:keep_frames]
        manifest.frames.count = len(manifest.frames.items)
        manifest.frames.unique_count = sum(f.is_unique for f in manifest.frames.items)
    if media_path is not None:
        manifest.media = type(manifest.media)(**{**manifest.media.__dict__, "path": media_path})
    frame_directory = directory / "frames"
    frame_directory.mkdir(parents=True, exist_ok=True)
    for frame in manifest.frames.items:
        (frame_directory / frame.file).write_bytes(marker + frame.file.encode())
    save_manifest(manifest, directory)
    return manifest


def _interrupted_commit(
    job_id: str, *, completed_steps: int, media_path: str | None = None
) -> Path:
    """Reproduce the on-disk state of a commit killed after N of its 3 renames.

    The rebuilt job deliberately has FEWER frames than the old one (2 vs 4):
    the tester's specimen was a 15-frame job rebuilt under a 3-frame cap.
    """
    directory = jobs.job_dir(job_id)
    _materialize(job_id, directory, marker=b"old:", created_at=OLD, media_path=media_path)
    staging = directory / f"{jobs.REPROCESS_PREFIX}killed"
    _materialize(
        job_id, staging, marker=b"new:", created_at=NEW, keep_frames=2, media_path=media_path
    )
    live_frames = directory / "frames"
    staged_frames = staging / "frames"
    backup = staging / jobs.REPROCESS_BACKUP_FRAMES
    if completed_steps >= 1:
        os.replace(live_frames, backup)
    if completed_steps >= 2:
        os.replace(staged_frames, live_frames)
    if completed_steps >= 3:
        os.replace(staging / "manifest.json", directory / "manifest.json")
    return staging


def _frame_bytes(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted((directory / "frames").iterdir())}


# --- F2: interrupted commit recovery ------------------------------------------


def test_kill_after_frame_swap_leaves_index_and_files_out_of_step(isolated_home: Path) -> None:
    """The specimen state: old manifest (4 frames indexed), new frames (2 files)."""
    job_id = "a1" * 8
    _interrupted_commit(job_id, completed_steps=2)
    stored = jobs.load_job(job_id)
    assert stored.created_at == OLD and stored.frames.count == 4
    assert len(jobs.missing_frame_files(stored)) == 2


def test_recovery_rolls_forward_a_commit_killed_after_the_frame_swap(
    isolated_home: Path,
) -> None:
    job_id = "a1" * 8
    staging = _interrupted_commit(job_id, completed_steps=2)
    recovered = jobs.recover_interrupted_reprocess(job_id)
    assert recovered == [jobs.ReprocessRecovery(f"{job_id}/{staging.name}", "rolled_forward")]
    live = jobs.load_job(job_id)
    assert live.created_at == NEW and live.frames.count == 2
    assert jobs.missing_frame_files(live) == []
    assert all(value.startswith(b"new:") for value in _frame_bytes(jobs.job_dir(job_id)).values())
    assert not staging.exists()


def test_recovery_finishes_a_commit_killed_before_the_frame_swap(isolated_home: Path) -> None:
    job_id = "b2" * 8
    staging = _interrupted_commit(job_id, completed_steps=1)
    assert not (jobs.job_dir(job_id) / "frames").exists()
    recovered = jobs.recover_interrupted_reprocess(job_id)
    assert [item.action for item in recovered] == ["rolled_forward"]
    live = jobs.load_job(job_id)
    assert live.created_at == NEW
    assert jobs.missing_frame_files(live) == []
    assert not staging.exists()


def test_recovery_removes_the_leftover_backup_of_a_completed_commit(
    isolated_home: Path,
) -> None:
    job_id = "c3" * 8
    staging = _interrupted_commit(job_id, completed_steps=3)
    recovered = jobs.recover_interrupted_reprocess(job_id)
    assert [item.action for item in recovered] == ["removed"]
    live = jobs.load_job(job_id)
    assert live.created_at == NEW and jobs.missing_frame_files(live) == []
    assert not staging.exists()


def test_recovery_rolls_back_when_the_staged_manifest_is_unusable(isolated_home: Path) -> None:
    job_id = "d4" * 8
    staging = _interrupted_commit(job_id, completed_steps=2)
    (staging / "manifest.json").write_text("{truncated", encoding="utf-8")
    recovered = jobs.recover_interrupted_reprocess(job_id)
    assert [item.action for item in recovered] == ["rolled_back"]
    live = jobs.load_job(job_id)
    assert live.created_at == OLD and live.frames.count == 4
    assert jobs.missing_frame_files(live) == []
    assert all(value.startswith(b"old:") for value in _frame_bytes(jobs.job_dir(job_id)).values())
    assert not staging.exists()


def test_recovery_leaves_abandoned_build_workspaces_to_gc(isolated_home: Path) -> None:
    """No previous-frames backup → the commit never started → not our case."""
    job_id = "e5" * 8
    staging = _interrupted_commit(job_id, completed_steps=0)
    assert jobs.recover_interrupted_reprocess(job_id) == []
    assert staging.is_dir()
    assert jobs.load_job(job_id).created_at == OLD


def test_gc_repairs_an_interrupted_commit_regardless_of_age(isolated_home: Path) -> None:
    """The 24 h litter rule must never throw away the only copy of the frames."""
    job_id = "f6" * 8
    staging = _interrupted_commit(job_id, completed_steps=2)
    young = jobs.job_dir("a7" * 8) / f"{jobs.REPROCESS_PREFIX}fresh-build"
    _materialize("a7" * 8, jobs.job_dir("a7" * 8), marker=b"old:", created_at=OLD)
    young.mkdir()
    (young / "audio.wav").write_bytes(b"partial")

    result = jobs.gc(keep_days=30)
    assert result.recovered == [
        jobs.ReprocessRecovery(f"{job_id}/{staging.name}", "rolled_forward")
    ]
    assert result.swept == []
    assert jobs.missing_frame_files(jobs.load_job(job_id)) == []
    assert young.is_dir(), "a young plain workspace may be a live build — untouched"


def test_gc_still_sweeps_old_abandoned_workspaces(isolated_home: Path) -> None:
    job_id = "a8" * 8
    staging = _interrupted_commit(job_id, completed_steps=0)
    stamp = time.time() - 3 * 86_400
    os.utime(staging, (stamp, stamp))
    result = jobs.gc(keep_days=30)
    assert result.swept == [f"{job_id}/{staging.name}"]
    assert result.recovered == []


def _stored_media_job(tmp_path: Path, *, completed_steps: int) -> tuple[Path, str, Path]:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"clip bytes that are only ever hashed")
    job_id = jobs.compute_job_id(media)
    staging = _interrupted_commit(job_id, completed_steps=completed_steps, media_path=str(media))
    return media, job_id, staging


def test_process_media_repairs_an_interrupted_commit_before_serving(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media, job_id, staging = _stored_media_job(tmp_path, completed_steps=2)

    def no_probe(path: Path):  # type: ignore[no-untyped-def]
        raise AssertionError("a repaired job is served, never rebuilt")

    monkeypatch.setattr(pipeline, "probe_media", no_probe)
    result = pipeline.process_media(str(media))
    assert result.reused is True
    assert result.manifest.created_at == NEW
    assert result.reprocess_recovered == (f"{job_id}/{staging.name} (rolled_forward)",)
    assert result.missing_frame_files == 0
    summary = pipeline.summarize(result)
    assert "rolled_forward" in summary["recovery_note"]
    assert "integrity_note" not in summary
    assert "missing_files" not in summary["frames"]


def test_process_media_reports_missing_frames_instead_of_a_clean_cache_hit(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media, job_id, _staging = _stored_media_job(tmp_path, completed_steps=0)
    (jobs.frames_dir(job_id) / "t00006006.jpg").unlink()
    monkeypatch.setattr(
        pipeline, "probe_media", lambda path: (_ for _ in ()).throw(AssertionError("no rebuild"))
    )
    result = pipeline.process_media(str(media))
    assert result.reused is True and result.missing_frame_files == 1
    summary = pipeline.summarize(result)
    assert summary["frames"]["missing_files"] == 1
    assert summary["integrity_note"].startswith("1 indexed keyframe file(s) are missing")
    assert "force=true)" in summary["integrity_note"]


def test_get_frames_and_get_moment_skip_missing_files_and_say_so(isolated_home: Path) -> None:
    from talkthrough_mcp.server import get_frames, get_moment

    job_id = "b9" * 8
    _materialize(job_id, jobs.job_dir(job_id), marker=b"old:", created_at=OLD)
    (jobs.frames_dir(job_id) / "t00006006.jpg").unlink()
    (jobs.frames_dir(job_id) / "t00012012.jpg").unlink()

    content = get_frames(job_id, start_ms=0, end_ms=20_000, max_frames=6, include_duplicates=True)
    meta, *images = content
    assert isinstance(meta, str)
    assert '"missing_frame_count": 2' in meta
    assert "2 indexed keyframe file(s) are missing" in meta
    assert '"returned": 2' in meta
    assert len(images) == 2

    moment, *moment_images = get_moment(job_id, 5000, 13_000)
    assert isinstance(moment, str)
    assert '"missing_frame_count": 2' in moment
    assert "integrity_note" in moment
    assert moment_images == []


# --- F1: an unreadable manifest is quarantined, never a bare error ------------


@pytest.mark.parametrize("damage", ["{not json", "{}", ""])
@pytest.mark.parametrize("force", [True, False])
def test_damaged_manifest_is_kept_beside_the_rebuilt_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, force: bool
) -> None:
    media, stored = _stored_force_identity_job(tmp_path, monkeypatch, video=True)
    _stub_successful_force(media, monkeypatch, video=True)
    directory = jobs.job_dir(stored.job_id)
    (directory / "manifest.json").write_text(damage, encoding="utf-8")

    result = pipeline.process_media(
        str(media), force=force, diarize_speakers=True, num_speakers=2
    )
    assert result.reused is False
    assert result.damaged_manifest_backup is not None
    backup = Path(result.damaged_manifest_backup)
    assert backup.parent == directory
    assert backup.name.startswith(f"manifest.json{jobs.DAMAGED_MANIFEST_SUFFIX}")
    assert backup.read_text(encoding="utf-8") == damage
    rebuilt = jobs.load_job(stored.job_id)
    assert rebuilt.tool_versions == {"talkthrough-mcp": "test"}
    assert rebuilt.transcript.diarization is not None
    assert rebuilt.transcript.diarization.speaker_names_pending_review is None
    assert jobs.missing_frame_files(rebuilt) == []
    assert not list(directory.glob(f"{jobs.REPROCESS_PREFIX}*"))
    summary = pipeline.summarize(result)
    assert backup.name in summary["manifest_recovery_note"]
    assert "NOT carried over" in summary["manifest_recovery_note"]


def test_failed_rebuild_of_a_damaged_job_keeps_the_damaged_file_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import stt

    media, stored = _stored_force_identity_job(tmp_path, monkeypatch, video=True)
    _stub_successful_force(media, monkeypatch, video=True)
    directory = jobs.job_dir(stored.job_id)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda *args, **kwargs: (_ for _ in ()).throw(ToolFailureError("controlled STT failure")),
    )
    with pytest.raises(ToolFailureError, match="controlled STT failure"):
        pipeline.process_media(str(media), force=True, diarize_speakers=True, num_speakers=2)
    assert (directory / "manifest.json").read_text(encoding="utf-8") == "{}"
    assert not list(directory.glob(f"manifest.json{jobs.DAMAGED_MANIFEST_SUFFIX}*"))
    assert not list(directory.glob(f"{jobs.REPROCESS_PREFIX}*"))
    assert directory.is_dir()


def test_load_previous_job_distinguishes_absent_from_unreadable(isolated_home: Path) -> None:
    assert jobs.load_previous_job("0" * 16) == (None, None)
    directory = jobs.job_dir("1" * 16)
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    manifest, reason = jobs.load_previous_job("1" * 16)
    assert manifest is None
    assert reason is not None and reason.startswith("KeyError")


def test_unexpected_exceptions_reach_the_agent_with_their_reason() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from talkthrough_mcp.server import _tool_errors

    with pytest.raises(ToolError, match=r"unexpected KeyError: 'media'"), _tool_errors():
        raise KeyError("media")


# --- F8: a rebuild's disk peak is checked up front ------------------------------


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


def test_disk_preflight_reserves_the_existing_keyframes_for_a_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from talkthrough_mcp.core.probe import MediaInfo

    info = MediaInfo(
        path="x", filename="x", size_bytes=1000, duration_s=5.0, has_video=True,
        has_audio=True, width=1, height=1, video_codec="h264", mtime_epoch=0.0,
    )
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda path: _Usage(free=2050))
    pipeline._validate_caps(info, tmp_path)
    with pytest.raises(ValidationError, match="existing keyframes that a rebuild keeps"):
        pipeline._validate_caps(info, tmp_path, reserved_bytes=100)


def test_forced_rebuild_passes_the_frames_directory_size_as_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media, stored = _stored_force_identity_job(tmp_path, monkeypatch, video=True)
    _stub_successful_force(media, monkeypatch, video=True)
    frames_bytes = sum(path.stat().st_size for path in jobs.frames_dir(stored.job_id).iterdir())
    assert frames_bytes > 0
    seen: list[int] = []
    real_validate = pipeline._validate_caps

    def spy(info, out_root, *, reserved_bytes=0):  # type: ignore[no-untyped-def]
        seen.append(reserved_bytes)
        real_validate(info, out_root, reserved_bytes=reserved_bytes)

    monkeypatch.setattr(pipeline, "_validate_caps", spy)
    pipeline.process_media(str(media), force=True, diarize_speakers=True, num_speakers=2)
    assert seen == [frames_bytes]
