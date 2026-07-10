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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import ToolFailureError, UnknownJobError
from .manifest import FRAMES_DIR_NAME, MANIFEST_NAME, Manifest, load_manifest

logger = logging.getLogger(__name__)

JOB_ID_LENGTH = 16
LOCK_NAME = "job.lock"
_HASH_CHUNK_BYTES = 1 << 20


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
    Windows is best-effort by design.
    """
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_NAME
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows best-effort
        yield
        return
    handle = lock_path.open("w")
    deadline = time.monotonic() + wait_seconds
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
        yield
    finally:
        handle.close()


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


def gc(keep_days: int) -> list[str]:
    """Delete jobs older than ``keep_days``; returns the removed job ids."""
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
    return removed
