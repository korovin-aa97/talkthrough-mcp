"""Content-addressed job store under ``~/.talkthrough/jobs/``.

``job_id = sha256(file bytes)[:16]`` — renaming or moving a recording never
triggers reprocessing, and the same file always maps to the same job. Each
job dir holds ``manifest.json``, ``frames/`` and a ``job.lock`` guarding
concurrent processing of the same file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import ToolFailureError, UnknownJobError
from .manifest import FRAMES_DIR_NAME, MANIFEST_NAME, SCHEMA, Manifest, load_manifest

logger = logging.getLogger(__name__)

JOB_ID_LENGTH = 16
LOCK_NAME = "job.lock"
_HASH_CHUNK_BYTES = 1 << 20
# A manifest-less job dir this old cannot be a live run (processing is capped
# at 2 h by default) — it is litter from a failure before cleanup existed.
PARTIAL_SWEEP_MIN_AGE_S = 24 * 3600.0
REPROCESS_PREFIX = ".reprocess-"
REPROCESS_BACKUP_FRAMES = "previous-frames"
REPROCESS_FAILED_FRAMES = "failed-new-frames"
# An unreadable manifest.json is never overwritten in place: a rebuild keeps
# it beside the fresh one under this suffix so hand-saved identities can still
# be recovered by a human (0.3.2 external finding F1).
DAMAGED_MANIFEST_SUFFIX = ".damaged-"
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def talkthrough_home() -> Path:
    override = os.environ.get("TALKTHROUGH_HOME")
    return Path(override).expanduser() if override else Path.home() / ".talkthrough"


def jobs_root() -> Path:
    return talkthrough_home() / "jobs"


def compute_job_id(media: Path) -> str:
    digest = hashlib.sha256()
    with media.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()[:JOB_ID_LENGTH]


def job_dir(job_id: str) -> Path:
    return jobs_root() / job_id


def frames_dir(job_id: str) -> Path:
    return job_dir(job_id) / FRAMES_DIR_NAME


def job_exists(job_id: str) -> bool:
    return (job_dir(job_id) / MANIFEST_NAME).is_file()


def load_job(job_id: str) -> Manifest:
    if not job_exists(job_id):
        raise UnknownJobError(job_id)
    return load_manifest(job_dir(job_id))


def load_previous_job(job_id: str) -> tuple[Manifest | None, str | None]:
    """Read a stored manifest for a rebuild decision, tolerating damage.

    Returns ``(manifest, None)`` when the file parses, ``(None, reason)`` when
    a ``manifest.json`` exists but cannot be read (truncated JSON, a hand
    edit, an empty file), and ``(None, None)`` when no manifest exists. A
    damaged manifest must neither crash the caller with a bare decoder error
    nor be mistaken for "no identities to preserve" without saying so.
    """
    if not job_exists(job_id):
        return None, None
    try:
        return load_manifest(job_dir(job_id)), None
    except Exception as exc:  # json.JSONDecodeError, KeyError, ValueError, ...
        reason = f"{type(exc).__name__}: {exc}"
        return None, reason[:200]


def damaged_manifest_backup_path(job_id: str) -> Path:
    """A fresh, collision-free name for quarantining an unreadable manifest."""
    directory = job_dir(job_id)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = directory / f"{MANIFEST_NAME}{DAMAGED_MANIFEST_SUFFIX}{stamp}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{MANIFEST_NAME}{DAMAGED_MANIFEST_SUFFIX}{stamp}-{counter}"
        counter += 1
    return candidate


def missing_frame_files(manifest: Manifest) -> list[str]:
    """Indexed keyframe files that are absent from the job's ``frames/`` dir.

    One directory listing per call — cheap enough for the read tools. A
    non-empty result means the stored index and the files on disk disagree
    (an interrupted rebuild, a manual deletion) and must be surfaced instead
    of silently serving fewer frames than the index promises.
    """
    if not manifest.frames.items:
        return []
    try:
        present = set(os.listdir(frames_dir(manifest.job_id)))
    except FileNotFoundError:
        present = set()
    return [frame.file for frame in manifest.frames.items if frame.file not in present]


def has_identity_state(manifest: Manifest) -> bool:
    """Whether a full rebuild could discard human-reviewed speaker data."""
    diarization = manifest.transcript.diarization
    if diarization is None:
        return False
    return any(
        (
            diarization.speaker_names,
            diarization.speaker_name_evidence,
            diarization.speaker_names_pending_review,
            diarization.speaker_name_evidence_pending_review,
            diarization.speaker_names_pending_review_context,
        )
    )


@contextmanager
def reprocess_workspace(job_id: str) -> Iterator[Path]:
    """Yield a hidden same-filesystem staging directory for a full rebuild.

    The caller holds :func:`job_lock`. Keeping staging inside the live job
    directory makes directory renames atomic while ensuring ``list_jobs``
    never mistakes an incomplete rebuild for a separate job.
    """
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=REPROCESS_PREFIX, dir=directory))
    try:
        yield staging
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validate_staged_job(job_id: str, staging: Path) -> Manifest:
    """Reload and validate the bounded invariants needed before commit."""
    try:
        manifest = load_manifest(staging)
    except Exception as exc:
        raise ToolFailureError(f"staged reprocess manifest is unreadable: {exc}") from exc
    if manifest.job_id != job_id:
        raise ToolFailureError(
            f"staged reprocess job_id {manifest.job_id!r} does not match {job_id!r}"
        )
    if manifest.schema != SCHEMA:
        raise ToolFailureError(
            f"staged reprocess schema {manifest.schema!r} does not match {SCHEMA!r}"
        )
    if manifest.frames.count != len(manifest.frames.items):
        raise ToolFailureError("staged reprocess frame count does not match its index")
    unique_count = sum(frame.is_unique for frame in manifest.frames.items)
    if manifest.frames.unique_count != unique_count:
        raise ToolFailureError("staged reprocess unique frame count does not match its index")
    staged_frames = staging / FRAMES_DIR_NAME
    for frame in manifest.frames.items:
        relative = Path(frame.file)
        if relative.is_absolute() or relative.name != frame.file:
            raise ToolFailureError(
                f"staged reprocess frame path is not a safe filename: {frame.file!r}"
            )
        if not (staged_frames / relative).is_file():
            raise ToolFailureError(
                f"staged reprocess frame is missing from its index: {frame.file!r}"
            )

    diarization = manifest.transcript.diarization
    if diarization is not None:
        active_names = diarization.speaker_names or {}
        pending_names = diarization.speaker_names_pending_review or {}
        if not set(diarization.speaker_name_evidence or {}) <= set(active_names):
            raise ToolFailureError("staged active speaker evidence has no matching name")
        if not set(diarization.speaker_name_evidence_pending_review or {}) <= set(
            pending_names
        ):
            raise ToolFailureError("staged pending speaker evidence has no matching name")
        if not set(diarization.speaker_names_pending_review_context or {}) <= set(
            pending_names
        ):
            raise ToolFailureError("staged pending speaker context has no matching name")
    return manifest


def commit_reprocessed_job(
    job_id: str, staging: Path, *, damaged_manifest_backup: Path | None = None
) -> Manifest:
    """Atomically publish a validated staged rebuild, rolling frames back on error.

    The caller must hold :func:`job_lock`. The old manifest remains present
    until the last ``os.replace``; catchable failures before that point restore
    the old frames and leave the old manifest authoritative.

    ``damaged_manifest_backup`` names where an UNREADABLE live manifest is
    moved right before the new one is published, so a rebuild of a damaged job
    never destroys the only copy of whatever that file still holds. If the
    publication then fails, the damaged file is moved back so the job keeps
    its "has a manifest" shape (which is what protects it from partial-dir
    cleanup).
    """
    manifest = _validate_staged_job(job_id, staging)
    directory = job_dir(job_id)
    live_manifest = directory / MANIFEST_NAME
    staged_manifest = staging / MANIFEST_NAME
    live_frames = directory / FRAMES_DIR_NAME
    staged_frames = staging / FRAMES_DIR_NAME
    backup_frames = staging / REPROCESS_BACKUP_FRAMES
    failed_frames = staging / REPROCESS_FAILED_FRAMES
    old_frames_moved = False
    new_frames_moved = False
    damaged_moved = False
    manifest_replaced = False
    try:
        if live_frames.exists():
            os.replace(live_frames, backup_frames)
            old_frames_moved = True
        if staged_frames.exists():
            os.replace(staged_frames, live_frames)
            new_frames_moved = True
        if damaged_manifest_backup is not None and live_manifest.exists():
            os.replace(live_manifest, damaged_manifest_backup)
            damaged_moved = True
        os.replace(staged_manifest, live_manifest)
        manifest_replaced = True
    except BaseException as exc:
        if not manifest_replaced:
            try:
                if damaged_moved and damaged_manifest_backup is not None:
                    os.replace(damaged_manifest_backup, live_manifest)
                if new_frames_moved and live_frames.exists():
                    os.replace(live_frames, failed_frames)
                if old_frames_moved and backup_frames.exists():
                    os.replace(backup_frames, live_frames)
                shutil.rmtree(failed_frames, ignore_errors=True)
            except Exception as rollback_exc:
                raise ToolFailureError(
                    "reprocess commit failed and frame rollback also failed: "
                    f"{rollback_exc}"
                ) from rollback_exc
        if isinstance(exc, Exception):
            raise ToolFailureError(
                "reprocess commit failed before manifest publication; the previous "
                "job was restored — retry the operation"
            ) from exc
        raise
    return manifest


@dataclass(frozen=True)
class ReprocessRecovery:
    """One interrupted rebuild that was finished or undone on lock acquisition.

    ``action`` is ``"rolled_forward"`` (the staged rebuild was published),
    ``"rolled_back"`` (the previous frames were restored) or ``"removed"``
    (the commit had already completed; only its backup was left behind).
    """

    staging: str
    action: str


def _frames_present(manifest: Manifest, frames_directory: Path) -> bool:
    try:
        present = set(os.listdir(frames_directory))
    except FileNotFoundError:
        present = set()
    return all(frame.file in present for frame in manifest.frames.items)


def _recover_one_staging(job_id: str, staging: Path) -> str | None:
    """Finish or undo one commit that a hard kill interrupted mid-sequence.

    ``commit_reprocessed_job`` renames in three steps — live frames → backup,
    staged frames → live, staged manifest → live. A backup directory inside
    the workspace proves the sequence had started, which is the one state the
    ordinary "abandoned workspace" sweep must never treat as litter: the live
    job is inconsistent until the sequence is either completed or reversed.
    Both halves of the answer sit in the workspace (0.3.2 external finding
    F2). Returns the action taken, or None for a workspace whose commit never
    began (a plain abandoned build, left for the age-based gc sweep).
    """
    directory = job_dir(job_id)
    live_manifest = directory / MANIFEST_NAME
    live_frames = directory / FRAMES_DIR_NAME
    staged_manifest = staging / MANIFEST_NAME
    staged_frames = staging / FRAMES_DIR_NAME
    backup_frames = staging / REPROCESS_BACKUP_FRAMES
    if not backup_frames.is_dir():
        return None
    staged: Manifest | None = None
    if staged_manifest.is_file():
        try:
            staged = load_manifest(staging)
        except Exception as exc:
            logger.warning("interrupted rebuild %s/%s: staged manifest unreadable: %s",
                           job_id, staging.name, exc)
            staged = None
        if staged is not None and staged.job_id != job_id:
            staged = None
    if staged is not None:
        if _frames_present(staged, live_frames):
            # Steps 1 and 2 completed; only the manifest publication is missing.
            os.replace(staged_manifest, live_manifest)
            shutil.rmtree(staging, ignore_errors=True)
            return "rolled_forward"
        if (
            staged_frames.is_dir()
            and not live_frames.exists()
            and _frames_present(staged, staged_frames)
        ):
            # Step 1 completed, step 2 did not: finish the sequence.
            os.replace(staged_frames, live_frames)
            os.replace(staged_manifest, live_manifest)
            shutil.rmtree(staging, ignore_errors=True)
            return "rolled_forward"
    if not staged_manifest.exists() and not staged_frames.exists() and live_manifest.is_file():
        # The whole sequence completed; the kill landed before the workspace
        # was removed. Nothing to restore — the backup is the old frame set.
        shutil.rmtree(staging, ignore_errors=True)
        return "removed"
    # The staged rebuild cannot be published: put the previous frames back so
    # the (still old) live manifest is consistent again.
    failed_frames = staging / REPROCESS_FAILED_FRAMES
    if live_frames.exists():
        shutil.rmtree(failed_frames, ignore_errors=True)
        os.replace(live_frames, failed_frames)
    os.replace(backup_frames, live_frames)
    shutil.rmtree(staging, ignore_errors=True)
    return "rolled_back"


def recover_interrupted_reprocess(job_id: str) -> list[ReprocessRecovery]:
    """Repair every interrupted rebuild of one job. Call under :func:`job_lock`.

    Holding the lock guarantees no rebuild is in flight, so any workspace
    whose commit had started is by definition an interrupted one. Workspaces
    whose commit never began are left to the age-based gc sweep.
    """
    directory = job_dir(job_id)
    if not directory.is_dir():
        return []
    recovered: list[ReprocessRecovery] = []
    for staging in sorted(directory.glob(f"{REPROCESS_PREFIX}*")):
        if not staging.is_dir():
            continue
        try:
            action = _recover_one_staging(job_id, staging)
        except OSError as exc:
            logger.warning(
                "could not recover interrupted rebuild %s/%s: %s", job_id, staging.name, exc
            )
            continue
        if action is not None:
            logger.warning("interrupted rebuild %s/%s: %s", job_id, staging.name, action)
            recovered.append(ReprocessRecovery(f"{job_id}/{staging.name}", action))
    return recovered


@contextmanager
def job_lock(job_id: str, *, wait_seconds: int = 600) -> Iterator[None]:
    """Exclusive per-job lock for threads and, where supported, processes.

    The process-local layer also gives Windows callers real thread safety;
    POSIX then adds ``flock`` for separate server/CLI processes.
    """
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(job_id, threading.RLock())
    if not process_lock.acquire(timeout=max(0, wait_seconds)):
        raise ToolFailureError(
            f"another thread has been holding the lock for job {job_id!r} "
            f"for {wait_seconds}s — retry later"
        )
    try:
        with _file_job_lock(job_id, wait_seconds=wait_seconds):
            yield
    finally:
        process_lock.release()


@contextmanager
def _file_job_lock(job_id: str, *, wait_seconds: int) -> Iterator[None]:
    """Cross-process half of :func:`job_lock`.

    POSIX flock; on platforms without fcntl (Windows) it degrades to a no-op —
    the outer process lock still serializes threads. After acquiring, the lock file's identity
    is re-checked: the holder we waited on may have failed and cleaned up the
    whole partial job directory (``cleanup_partial_job``) — then the flock we
    hold is on an orphaned inode, and it must be retaken on the fresh path so
    two waiters can never both "win" on different inodes. Every retake
    iteration honors the same ``wait_seconds`` deadline the flock wait does
    (v0.2.6) — a lock file that keeps vanishing must end in a clean error,
    not an unbounded busy loop.
    """
    directory = job_dir(job_id)
    lock_path = directory / LOCK_NAME
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows best-effort
        # No flock semantics, but keep the on-disk layout identical: the
        # job.lock marker exists on every platform.
        directory.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
        yield
        return

    def check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ToolFailureError(
                f"could not acquire the lock for job {job_id!r} within {wait_seconds}s "
                "— another process keeps recreating it; retry later"
            ) from None

    deadline = time.monotonic() + wait_seconds
    while True:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            handle = lock_path.open("w")
        except FileNotFoundError:
            # the directory vanished between mkdir and open (a concurrent
            # cleanup_partial_job) — retry on a fresh directory instead of
            # leaking a raw FileNotFoundError to the caller
            check_deadline(deadline)
            time.sleep(0.05)
            continue
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ToolFailureError(
                            f"another process has been holding the lock for job {job_id!r} "
                            f"for {wait_seconds}s — retry later"
                        ) from None
                    time.sleep(1)
        except BaseException:
            handle.close()
            raise
        try:
            same_file = os.stat(lock_path).st_ino == os.fstat(handle.fileno()).st_ino
        except FileNotFoundError:
            same_file = False
        if same_file:
            break
        handle.close()  # lock file vanished/was replaced under us — retake
        check_deadline(deadline)
        time.sleep(0.05)
    try:
        yield
    finally:
        handle.close()


def cleanup_partial_job(job_id: str) -> bool:
    """Remove a job directory that failed before its manifest was written.

    Call while holding the job lock. A directory WITH a manifest is never
    touched — completed jobs and amend targets stay intact. Returns True when
    a partial directory was removed. The caller's own lock handle survives
    (flock on the unlinked inode); blocked waiters detect the vanished lock
    file and retake it on a fresh one (see ``job_lock``).
    """
    directory = job_dir(job_id)
    if not directory.is_dir() or (directory / MANIFEST_NAME).is_file():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


@contextmanager
def partial_job_cleanup(job_id: str) -> Iterator[None]:
    """Delete the partial job dir when the wrapped processing fails.

    Stack INSIDE ``job_lock`` (cleanup must happen while still holding the
    lock). A failure before the manifest exists — cold-start model download,
    cap validation, an ffmpeg crash — otherwise leaves a job directory with
    only ``job.lock`` behind: invisible to ``list_jobs`` and harmless, but
    litter. A failure after the manifest exists (e.g. a diarization amend)
    removes nothing.
    """
    try:
        yield
    except BaseException:
        cleanup_partial_job(job_id)
        raise


def _list_jobs(*, warn_missing_manifest: bool) -> list[Manifest]:
    """Implementation hook letting gc defer expected partial-dir reporting."""
    root = jobs_root()
    if not root.is_dir():
        return []
    manifests: list[Manifest] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            manifests.append(load_manifest(directory))
        except FileNotFoundError as exc:
            if warn_missing_manifest:
                logger.warning("skipping unreadable job dir %s: %s", directory.name, exc)
        except Exception as exc:
            logger.warning("skipping unreadable job dir %s: %s", directory.name, exc)
    manifests.sort(key=lambda manifest: manifest.created_at, reverse=True)
    return manifests


def list_jobs() -> list[Manifest]:
    """All readable job manifests, newest first. Unreadable job dirs are skipped."""
    return _list_jobs(warn_missing_manifest=True)


def delete_job(job_id: str) -> None:
    directory = job_dir(job_id)
    if directory.is_dir():
        shutil.rmtree(directory)


@dataclass(frozen=True)
class GcResult:
    """What one ``gc`` pass did: completed jobs deleted by age, plus
    manifest-less partial directories swept (litter from failures that
    predate the in-run cleanup — invisible to ``list_jobs`` by construction,
    so the age pass alone can never reach them)."""

    removed: list[str]
    swept: list[str]
    # Interrupted rebuild commits finished or undone before any sweep — a
    # workspace holding the previous frames is never plain litter.
    recovered: list[ReprocessRecovery] = field(default_factory=list)


def _sweep_partial_dirs(min_age_s: float = PARTIAL_SWEEP_MIN_AGE_S) -> list[str]:
    """Remove manifest-less job directories older than ``min_age_s``.

    Each candidate is taken under its own job lock, non-blocking — a held
    lock means a live run owns the directory, and a live run is never swept.
    The removal itself goes through ``cleanup_partial_job``, which re-checks
    under the lock that no manifest exists.
    """
    root = jobs_root()
    if not root.is_dir():
        return []
    now = time.time()
    swept: list[str] = []
    for directory in root.iterdir():
        if not directory.is_dir() or (directory / MANIFEST_NAME).is_file():
            continue
        try:
            age_s = now - directory.stat().st_mtime
        except OSError:
            continue  # vanished mid-scan
        if age_s < min_age_s:
            logger.info(
                "found partial dir %s, left in place (younger than %.0f hours)",
                directory.name,
                min_age_s / 3600,
            )
            continue
        try:
            with job_lock(directory.name, wait_seconds=0):
                if cleanup_partial_job(directory.name):
                    swept.append(directory.name)
                    logger.info("found partial dir %s, swept", directory.name)
        except ToolFailureError:
            logger.info("found partial dir %s, left in place (job lock is held)", directory.name)
            continue  # a live run holds the lock — leave its directory alone
    return swept


def _sweep_reprocess_dirs(
    min_age_s: float = PARTIAL_SWEEP_MIN_AGE_S,
) -> tuple[list[str], list[ReprocessRecovery]]:
    """Repair interrupted rebuilds, then remove abandoned workspaces.

    Recovery is age-independent: a workspace holding ``previous-frames`` means
    a commit was interrupted and the live job is inconsistent right now.
    Plain abandoned build workspaces keep the age rule — a young one may be a
    run that just lost the lock race, and the lock probe below skips live
    runs entirely.
    """
    root = jobs_root()
    if not root.is_dir():
        return [], []
    now = time.time()
    swept: list[str] = []
    recovered: list[ReprocessRecovery] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        for staging in directory.glob(f"{REPROCESS_PREFIX}*"):
            if not staging.is_dir():
                continue
            try:
                with job_lock(directory.name, wait_seconds=0):
                    if not staging.is_dir():
                        continue
                    if (staging / REPROCESS_BACKUP_FRAMES).is_dir():
                        try:
                            action = _recover_one_staging(directory.name, staging)
                        except OSError as exc:
                            logger.warning(
                                "could not recover interrupted rebuild %s/%s: %s",
                                directory.name,
                                staging.name,
                                exc,
                            )
                            continue
                        if action is not None:
                            recovered.append(
                                ReprocessRecovery(f"{directory.name}/{staging.name}", action)
                            )
                            continue
                    try:
                        if now - staging.stat().st_mtime < min_age_s:
                            continue
                    except OSError:
                        continue
                    shutil.rmtree(staging)
                    swept.append(f"{directory.name}/{staging.name}")
            except ToolFailureError:
                logger.info(
                    "found stale reprocess dir %s/%s, left in place (job lock is held)",
                    directory.name,
                    staging.name,
                )
    return swept, recovered


def gc(keep_days: int) -> GcResult:
    """Delete jobs older than ``keep_days`` and sweep stale partial dirs."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed: list[str] = []
    # A missing manifest is precisely the partial-dir class handled by the
    # sweep below, so gc defers that expected case instead of warning first.
    # Corrupt manifests remain warnings: the sweep deliberately preserves them.
    for manifest in _list_jobs(warn_missing_manifest=False):
        try:
            created = datetime.fromisoformat(manifest.created_at)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            delete_job(manifest.job_id)
            removed.append(manifest.job_id)
    swept_reprocess, recovered = _sweep_reprocess_dirs()
    return GcResult(
        removed=removed,
        swept=[*_sweep_partial_dirs(), *swept_reprocess],
        recovered=recovered,
    )
