"""process_url end to end without a network: a stub downloader hands the
orchestration a file, the real pipeline runs on stubbed stages, and every
surface (manifest, index, summary, tool, CLI) is checked for the contract —
one download per URL, content-addressed reuse, no raw URL anywhere."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import jobs, pipeline, url_download, url_ingest
from talkthrough_mcp.core.probe import MediaInfo
from talkthrough_mcp.core.url_download import Downloaded
from talkthrough_mcp.core.url_ingest import DownloadError, process_url

CANARY = "sig=SECRET-TOKEN-4242"
URL = f"https://cdn.example.com/recordings/standup.m4a?{CANARY}"
MEDIA = b"\x00\x00\x00\x1cftypM4A " + b"a" * 2000


def _fake_probe(path: Path) -> MediaInfo:
    stat = Path(path).stat()
    return MediaInfo(
        path=str(path), filename=Path(path).name, size_bytes=stat.st_size, duration_s=8.0,
        has_video=False, has_audio=True, width=0, height=0, video_codec="",
        mtime_epoch=stat.st_mtime, format_tags={},
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> dict[str, Any]:
    """Real pipeline + real orchestration; only the network and the models are fake."""
    from talkthrough_mcp.core import audio, probe, stt

    calls: dict[str, Any] = {"downloads": 0, "media": MEDIA, "delay": 0.0}

    def fake_download(source: Any, dest_dir: Path, *, max_bytes: int, report: Any) -> Downloaded:
        calls["downloads"] += 1
        time.sleep(calls["delay"])
        assert max_bytes == url_ingest.max_download_bytes()
        report("downloading source: 0.0/0.0 MB", 0.1)
        path = dest_dir / "download.m4a"
        path.write_bytes(calls["media"])
        return Downloaded(
            path=path, extension=".m4a", downloaded_bytes=len(calls["media"]),
            downloader="stub 1.0", validators={"etag": '"e1"'},
        )

    monkeypatch.setattr(url_download, "download_direct", fake_download)
    monkeypatch.setattr(probe, "probe_media", _fake_probe)
    monkeypatch.setattr(pipeline, "probe_media", _fake_probe)
    monkeypatch.setattr(audio, "extract_wav", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda *args, **kwargs: stt.SttResult(
            language="en", model="tiny",
            segments=tuple(make_manifest(kind="audio").transcript.segments[:2]), latency_ms=1,
        ),
    )
    monkeypatch.setattr(pipeline, "_tool_versions", lambda: {"talkthrough-mcp": "test"})
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    monkeypatch.delenv("TALKTHROUGH_MAX_DOWNLOAD_BYTES", raising=False)
    return calls


def _all_store_text(home: Path) -> str:
    chunks = []
    for path in sorted(home.rglob("*")):
        if path.is_file() and path.suffix in {".json", ".lock", ".tmp"}:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def test_process_url_downloads_once_then_serves_the_stored_job_without_network(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    stages: list[tuple[str, float]] = []
    first = process_url(URL, progress=lambda stage, fraction: stages.append((stage, fraction)))
    assert stubbed["downloads"] == 1
    assert first.reused_url_mapping is False and first.refreshed is False
    assert first.downloaded_bytes == len(MEDIA)
    result = first.result
    assert result.reused is False
    manifest = result.manifest
    assert manifest.media.origin is not None
    assert manifest.media.origin.kind == "direct_url"
    assert manifest.media.origin.provider == "cdn.example.com"
    assert manifest.media.origin.downloader == "stub 1.0"
    assert manifest.media.managed_source is not None
    assert manifest.media.managed_source.startswith("source/direct-")
    managed = jobs.job_dir(manifest.job_id) / manifest.media.managed_source
    assert managed.is_file() and managed.read_bytes() == MEDIA
    assert Path(manifest.media.path) == managed
    assert manifest.wall_clock is None, "download mtime must never anchor t_wall"
    assert stages[0][0] == "validating URL"
    assert any(stage.startswith("downloading source") for stage, _ in stages)
    assert any(stage == "verifying media" for stage, _ in stages)
    pipeline_fractions = [fraction for stage, fraction in stages if stage == "writing manifest"]
    assert pipeline_fractions and all(fraction >= 0.15 for fraction in pipeline_fractions)
    assert all(CANARY not in stage for stage, _ in stages)
    assert CANARY not in _all_store_text(isolated_home)
    assert "standup" not in _all_store_text(isolated_home)
    assert url_ingest.load_mapping(first.source.mapping_key) is not None
    assert not any(url_ingest.downloads_root().iterdir())

    second = process_url(URL)
    assert stubbed["downloads"] == 1, "a known URL must not touch the network"
    assert second.reused_url_mapping is True and second.downloaded_bytes is None
    assert second.result.reused is True
    assert second.result.manifest.job_id == manifest.job_id

    third = process_url(URL, refresh=True)
    assert stubbed["downloads"] == 2
    assert third.refreshed is True and third.reused_url_mapping is False
    assert third.result.manifest.job_id == manifest.job_id  # same bytes → same job
    assert third.result.reused is True


def test_process_url_summary_carries_origin_and_a_wall_clock_note(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    ingested = process_url(URL)
    summary = pipeline.summarize(ingested.result)
    origin = summary["origin"]
    assert origin["kind"] == "direct_url" and origin["provider"] == "cdn.example.com"
    assert origin["managed_source"].startswith("source/")
    assert "downloaded_bytes" in origin and "downloaded_at" in origin
    assert CANARY not in json.dumps(summary)
    assert "standup" not in json.dumps(summary)


def test_process_url_converges_on_a_job_processed_from_a_local_file(
    stubbed: dict[str, Any], isolated_home: Path, tmp_path: Path
) -> None:
    local = tmp_path / "standup.m4a"
    local.write_bytes(MEDIA)
    local_result = pipeline.process_media(str(local))
    assert local_result.manifest.media.origin is None
    assert local_result.manifest.wall_clock is not None  # a local file keeps the mtime rung

    ingested = process_url(URL)
    assert stubbed["downloads"] == 1
    assert ingested.result.reused is True
    manifest = ingested.result.manifest
    assert manifest.job_id == local_result.manifest.job_id
    assert manifest.media.path == str(local.resolve())
    assert manifest.media.origin is not None and manifest.media.origin.kind == "direct_url"
    assert manifest.media.managed_source is not None
    managed = jobs.job_dir(manifest.job_id) / manifest.media.managed_source
    assert managed.read_bytes() == MEDIA
    assert url_ingest.source_path(
        manifest.media.path, manifest.job_id, manifest.media.managed_source
    ) == managed
    stored = jobs.load_job(manifest.job_id)
    assert stored.media.managed_source == manifest.media.managed_source
    assert stored.wall_clock is not None and stored.wall_clock.source == "mtime"


def test_process_url_redownloads_when_the_managed_source_is_gone(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    first = process_url(URL)
    manifest = first.result.manifest
    assert manifest.media.managed_source is not None
    (jobs.job_dir(manifest.job_id) / manifest.media.managed_source).unlink()
    second = process_url(URL)
    assert stubbed["downloads"] == 2
    assert second.reused_url_mapping is False
    assert (jobs.job_dir(manifest.job_id) / manifest.media.managed_source).is_file()


def test_process_url_recorded_at_anchors_wall_clock(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    ingested = process_url(URL, recorded_at="2026-09-05T14:00:00+02:00")
    wall = ingested.result.manifest.wall_clock
    assert wall is not None and wall.source == "override" and wall.confidence == "exact"


def test_process_url_failure_leaves_no_job_no_mapping_no_staging(
    stubbed: dict[str, Any], isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: Any, dest_dir: Path, *, max_bytes: int, report: Any) -> Downloaded:
        (dest_dir / "download.m4a.part").write_bytes(b"partial")
        raise DownloadError("network error while downloading https://x/… (direct media URL)")

    monkeypatch.setattr(url_download, "download_direct", failing)
    with pytest.raises(DownloadError):
        process_url(URL)
    assert not jobs.jobs_root().exists() or not any(jobs.jobs_root().iterdir())
    assert url_ingest.load_mapping(url_ingest.classify_url(URL).mapping_key) is None
    assert not any(url_ingest.downloads_root().iterdir())


def test_process_url_rejects_a_non_media_download_before_the_job_store(
    stubbed: dict[str, Any], isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import probe
    from talkthrough_mcp.core.errors import ValidationError

    def no_streams(path: Path) -> MediaInfo:
        raise ValidationError("no audio or video streams found in 'download.m4a'")

    monkeypatch.setattr(probe, "probe_media", no_streams)
    with pytest.raises(ValidationError, match="no audio or video streams"):
        process_url(URL)
    assert not jobs.jobs_root().exists() or not any(jobs.jobs_root().iterdir())
    assert not any(url_ingest.downloads_root().iterdir())


def test_process_url_duration_cap_applies_to_downloads(
    stubbed: dict[str, Any], isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core.errors import ValidationError

    monkeypatch.setenv("TALKTHROUGH_MAX_SECONDS", "5")
    with pytest.raises(ValidationError, match="exceeds the 5s cap"):
        process_url(URL)
    assert not jobs.jobs_root().exists() or not any(jobs.jobs_root().iterdir())


def test_concurrent_calls_on_one_url_download_once(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    stubbed["delay"] = 0.2
    barrier = threading.Barrier(2)

    def call() -> Any:
        barrier.wait(timeout=5)
        return process_url(URL)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(call), pool.submit(call)]]
    assert stubbed["downloads"] == 1
    assert {item.result.manifest.job_id for item in results} == {results[0].result.manifest.job_id}
    assert sorted(item.reused_url_mapping for item in results) == [False, True]


def test_two_urls_with_the_same_bytes_are_one_job(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    first = process_url(URL)
    second = process_url("https://mirror.example.org/copy.m4a")
    assert stubbed["downloads"] == 2
    assert first.result.manifest.job_id == second.result.manifest.job_id
    assert second.result.reused is True
    stored = jobs.load_job(first.result.manifest.job_id)
    assert stored.media.origin is not None
    assert stored.media.origin.provider == "cdn.example.com", "the first origin is kept"
    assert len(list(url_ingest.urls_root().glob("*.json"))) == 2


def test_gc_removes_the_managed_source_with_the_job_and_its_url_mapping(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    ingested = process_url(URL)
    job_id = ingested.result.manifest.job_id
    jobs.delete_job(job_id)
    result = jobs.gc(keep_days=30)
    assert result.swept == [f"urls/{url_ingest.mapping_path(ingested.source.mapping_key).name}"]
    assert not (jobs.job_dir(job_id) / "source").exists()


def test_forced_rebuild_of_a_url_job_keeps_its_origin(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    ingested = process_url(URL)
    manifest = ingested.result.manifest
    rebuilt = pipeline.process_media(manifest.media.path, force=True)
    assert rebuilt.reused is False
    assert rebuilt.manifest.media.origin == manifest.media.origin
    assert rebuilt.manifest.media.managed_source == manifest.media.managed_source
    assert rebuilt.manifest.wall_clock is None


# --- the tool and the CLI ---------------------------------------------------------


class _Ctx:
    def __init__(self) -> None:
        self.messages: list[tuple[float, str]] = []

    async def report_progress(self, progress: float, total: float, message: str) -> None:
        self.messages.append((progress, message))


def test_process_url_tool_summary(stubbed: dict[str, Any], isolated_home: Path) -> None:
    from talkthrough_mcp.server import process_url as tool

    ctx = _Ctx()
    summary = asyncio.run(tool(URL, ctx))  # type: ignore[arg-type]
    assert summary["origin"]["reused_url_mapping"] is False
    assert summary["origin"]["refreshed"] is False
    assert summary["origin"]["network"].startswith("downloaded ")
    assert "wall_clock_note" in summary
    assert CANARY not in json.dumps(summary)
    assert all(CANARY not in message for _, message in ctx.messages)
    assert ctx.messages[0][1].startswith("ingesting a URL")

    again = asyncio.run(tool(URL, _Ctx()))  # type: ignore[arg-type]
    assert again["origin"]["reused_url_mapping"] is True
    assert again["origin"]["network"].startswith("none")
    assert again["reused"] is True


def test_process_url_tool_errors_are_clean_and_redacted(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from talkthrough_mcp.server import process_url as tool

    with pytest.raises(ToolError, match="local path") as info:
        asyncio.run(tool("/Users/sam/clip.mov", _Ctx()))  # type: ignore[arg-type]
    assert "process_media" in str(info.value)
    with pytest.raises(ToolError, match="credentials") as info:
        asyncio.run(tool(f"https://u:hunter2@cdn.example.com/x.mp4?{CANARY}", _Ctx()))  # type: ignore[arg-type]
    assert "hunter2" not in str(info.value) and CANARY not in str(info.value)


def test_cli_process_url_prints_json_to_stdout_only(
    stubbed: dict[str, Any], isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from talkthrough_mcp.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["process-url", URL, "--json"])
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["origin"]["kind"] == "direct_url"
    assert payload["origin"]["reused_url_mapping"] is False
    assert CANARY not in captured.out and CANARY not in captured.err
    assert "downloading source" in captured.err

    with pytest.raises(SystemExit):
        main(["process-url", URL])
    human = capsys.readouterr().out
    assert "origin     : direct_url cdn.example.com  (reused stored job, no network)" in human


def test_cli_process_url_reports_failures_as_exit_2(
    stubbed: dict[str, Any], isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from talkthrough_mcp.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["process-url", "https://www.youtube.com/playlist?list=PLxyz"])
    assert exit_info.value.code == 2
    assert "playlist" in capsys.readouterr().err


def test_extract_frame_prefers_the_managed_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from talkthrough_mcp import server
    from talkthrough_mcp.core.manifest import MediaOrigin, save_manifest

    manifest = make_manifest()
    manifest.media = replace(
        manifest.media,
        path="/gone/original.mp4",
        origin=MediaOrigin(kind="youtube", provider="youtube", url_sha256="ab" * 32),
        managed_source="source/youtube-x.mp4",
    )
    directory = jobs.job_dir(manifest.job_id)
    (directory / "source").mkdir(parents=True)
    (directory / "source" / "youtube-x.mp4").write_bytes(b"video")
    save_manifest(manifest, directory)
    decoded: list[Path] = []

    def fake_extract(
        media: Path, at_ms: int, out_path: Path, *, crop: Any = None, **kw: Any
    ) -> None:
        decoded.append(media)
        out_path.write_bytes(b"\xff\xd8\xff\xd9")

    monkeypatch.setattr(server, "extract_exact_frame", fake_extract)
    meta, _image = server.extract_frame(manifest.job_id, at_ms=1500)
    assert decoded == [directory / "source" / "youtube-x.mp4"]
    assert isinstance(meta, str) and "youtube-x.mp4" in meta


def test_list_jobs_shows_the_origin_of_url_jobs(
    stubbed: dict[str, Any], isolated_home: Path
) -> None:
    from talkthrough_mcp.server import list_jobs

    process_url(URL)
    (entry,) = list_jobs()["jobs"]
    assert entry["origin"] == {"kind": "direct_url", "provider": "cdn.example.com"}
    assert CANARY not in json.dumps(entry)
