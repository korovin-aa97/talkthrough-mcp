"""Content-addressed job store: hashing, home override, listing, gc."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import jobs
from talkthrough_mcp.core.errors import UnknownJobError
from talkthrough_mcp.core.manifest import save_manifest


def test_job_id_is_content_hash_prefix(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake-video-bytes" * 1000)
    job_id = jobs.compute_job_id(media)
    assert job_id == hashlib.sha256(media.read_bytes()).hexdigest()[:16]

    renamed = tmp_path / "renamed.mp4"
    media.rename(renamed)
    assert jobs.compute_job_id(renamed) == job_id  # renames are free

    renamed.write_bytes(b"different-bytes")
    assert jobs.compute_job_id(renamed) != job_id


def test_home_override(isolated_home: Path) -> None:
    assert jobs.talkthrough_home() == isolated_home
    assert jobs.jobs_root() == isolated_home / "jobs"


def test_load_job_raises_for_unknown_id(isolated_home: Path) -> None:
    with pytest.raises(UnknownJobError, match="unknown9999"):
        jobs.load_job("unknown9999")


def _store_job(job_id: str, created_at: str) -> None:
    directory = jobs.job_dir(job_id)
    directory.mkdir(parents=True)
    save_manifest(make_manifest(job_id=job_id, created_at=created_at), directory)


def test_list_jobs_newest_first_and_skips_broken(isolated_home: Path) -> None:
    _store_job("aaaaaaaaaaaaaaaa", "2026-07-01T10:00:00+00:00")
    _store_job("bbbbbbbbbbbbbbbb", "2026-07-09T10:00:00+00:00")
    broken = jobs.jobs_root() / "cccccccccccccccc"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")

    listed = jobs.list_jobs()
    assert [manifest.job_id for manifest in listed] == [
        "bbbbbbbbbbbbbbbb",
        "aaaaaaaaaaaaaaaa",
    ]


def test_gc_removes_only_stale_jobs(isolated_home: Path) -> None:
    now = datetime.now(UTC)
    fresh = (now - timedelta(days=1)).isoformat(timespec="seconds")
    stale = (now - timedelta(days=45)).isoformat(timespec="seconds")
    _store_job("1111111111111111", fresh)
    _store_job("2222222222222222", stale)

    removed = jobs.gc(keep_days=30)
    assert removed == ["2222222222222222"]
    assert jobs.job_exists("1111111111111111")
    assert not jobs.job_exists("2222222222222222")


def test_job_lock_is_reentrant_across_jobs(isolated_home: Path) -> None:
    with jobs.job_lock("aaaaaaaaaaaaaaaa"), jobs.job_lock("bbbbbbbbbbbbbbbb"):
        assert (jobs.job_dir("aaaaaaaaaaaaaaaa") / "job.lock").exists()
