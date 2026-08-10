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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import ToolFailureError, UnknownJobError
from .manifest import FRAMES_DIR_NAME, MANIFEST_NAME, Manifest, load_manifest

logger = logging.getLogger(__name__)

JOB_ID_LENGTH = 16
LOCK_NAME = "job.lock"
_HASH_CHUNK_BYTES = 1 << 20
# A manifest-less job dir this old cannot be a live run (processing is capped
# at 2 h by default) — it is litter from a failure before cleanup existed.
PARTIAL_SWEEP_MIN_AGE_S = 24 * 3600.0


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


@contextmanager
def job_lock(job_id: str, *, wait_seconds: int = 600) -> Iterator[None]:
    """Exclusive per-job lock so two processes never preprocess the same file at once.

    POSIX flock; on platforms without fcntl (Windows) it degrades to a no-op —
    Windows is best-effort by design. After acquiring, the lock file's identity
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


def list_jobs() -> list[Manifest]:
    """All readable job manifests, newest first. Unreadable job dirs are skipped."""
    root = jobs_root()
    if not root.is_dir():
        return []
    manifests: list[Manifest] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            manifests.append(load_manifest(directory))
        except Exception as exc:
            logger.warning("skipping unreadable job dir %s: %s", directory.name, exc)
    manifests.sort(key=lambda manifest: manifest.created_at, reverse=True)
    return manifests


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
            continue
        try:
            with job_lock(directory.name, wait_seconds=0):
                if cleanup_partial_job(directory.name):
                    swept.append(directory.name)
        except ToolFailureError:
            continue  # a live run holds the lock — leave its directory alone
    return swept


def gc(keep_days: int) -> GcResult:
    """Delete jobs older than ``keep_days`` and sweep stale partial dirs."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed: list[str] = []
    for manifest in list_jobs():
        try:
            created = datetime.fromisoformat(manifest.created_at)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            delete_job(manifest.job_id)
            removed.append(manifest.job_id)
    return GcResult(removed=removed, swept=_sweep_partial_dirs())
