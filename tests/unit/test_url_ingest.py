"""URL ingestion, pure parts: classification, redaction, the destination
gate, the URL index and the additive manifest origin — no network."""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import jobs, url_ingest
from talkthrough_mcp.core.manifest import Manifest, MediaOrigin, load_manifest, save_manifest
from talkthrough_mcp.core.url_ingest import (
    DownloadError,
    UnsafeUrlError,
    UnsupportedUrlError,
    UrlMapping,
    classify_url,
    is_public_address,
    redact,
    resolve_public_host,
)

CANARY = "sig=SECRET-TOKEN-4242"

# --- classification ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=nHfGfEiVdE8",
        "https://youtube.com/watch?v=nHfGfEiVdE8&t=42s",
        "http://m.youtube.com/watch?v=nHfGfEiVdE8",
        "https://youtu.be/nHfGfEiVdE8",
        "https://youtu.be/nHfGfEiVdE8?si=abc123",
        "https://www.youtube.com/shorts/nHfGfEiVdE8",
        "https://www.youtube.com/live/nHfGfEiVdE8?feature=share",
        "https://www.youtube.com/embed/nHfGfEiVdE8",
        "https://www.youtube.com/watch?v=nHfGfEiVdE8&list=PLxyz&index=3",
        "https://music.youtube.com/watch?v=nHfGfEiVdE8",
        "  https://youtu.be/nHfGfEiVdE8  ",
    ],
)
def test_youtube_forms_collapse_to_one_canonical_video(url: str) -> None:
    source = classify_url(url)
    assert source.kind == "youtube"
    assert source.provider_id == "nHfGfEiVdE8"
    assert source.canonical_url == "https://www.youtube.com/watch?v=nHfGfEiVdE8"
    assert source.request_url == source.canonical_url
    assert source.mapping_key == "youtube:nHfGfEiVdE8"
    assert "list=" not in source.request_url and "si=" not in source.request_url


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("https://www.youtube.com/playlist?list=PLxyz", "playlist"),
        ("https://www.youtube.com/watch?list=PLxyz", "playlist"),
        ("https://www.youtube.com/@somechannel", "channel"),
        ("https://www.youtube.com/channel/UCxyz", "channel"),
        ("https://www.youtube.com/c/somechannel/videos", "channel"),
        ("https://www.youtube.com/user/someone", "channel"),
        ("https://www.youtube.com/results?search_query=cats", "search"),
        ("https://www.youtube.com/feed/subscriptions", "feed"),
        ("https://www.youtube.com/watch", "video id"),
        ("https://youtu.be/", "video id"),
        ("https://youtu.be/too-short", "unexpected shape"),
        ("https://www.youtube.com/shorts/", "unrecognized"),
    ],
)
def test_youtube_non_video_urls_are_refused_before_any_network(url: str, fragment: str) -> None:
    with pytest.raises(UnsupportedUrlError, match=fragment):
        classify_url(url)


def test_direct_https_url_keeps_query_in_memory_only_and_hashes_the_exact_input() -> None:
    url = f"https://cdn.example.com/media/clip.mp4?{CANARY}#t=10"
    source = classify_url(url)
    assert source.kind == "direct_url"
    assert source.provider == "cdn.example.com"
    assert source.host == "cdn.example.com"
    assert source.path_extension == ".mp4"
    assert source.request_url == f"https://cdn.example.com/media/clip.mp4?{CANARY}"
    assert source.mapping_key.startswith("direct:")
    assert len(source.url_sha256) == 64
    assert CANARY not in source.mapping_key and CANARY not in source.url_sha256
    assert source.provider_id is None
    assert CANARY in source.secrets
    assert CANARY not in source.safe_label()


def test_direct_url_without_media_extension_defers_to_the_response() -> None:
    source = classify_url("https://cdn.example.com/download?id=7")
    assert source.path_extension is None
    assert source.request_url == "https://cdn.example.com/download?id=7"


def test_two_different_direct_urls_get_two_mapping_keys() -> None:
    first = classify_url("https://cdn.example.com/a.mp4")
    second = classify_url("https://cdn.example.com/b.mp4")
    assert first.mapping_key != second.mapping_key
    assert classify_url("https://cdn.example.com/a.mp4").mapping_key == first.mapping_key


@pytest.mark.parametrize(
    ("url", "error", "fragment"),
    [
        ("", UnsupportedUrlError, "empty"),
        ("/Users/sam/clip.mov", UnsupportedUrlError, "local path"),
        ("~/clip.mov", UnsupportedUrlError, "local path"),
        (r"C:\Users\sam\clip.mov", UnsupportedUrlError, "local path"),
        ("file:///Users/sam/clip.mov", UnsupportedUrlError, "file://"),
        ("ftp://cdn.example.com/clip.mp4", UnsupportedUrlError, "scheme"),
        ("http://cdn.example.com/clip.mp4", UnsupportedUrlError, "http://"),
        ("https://cdn.example.com/a b.mp4", UnsupportedUrlError, "whitespace"),
        ("https://cdn.example.com/a\x00b.mp4", UnsupportedUrlError, "control"),
        ("https://", UnsupportedUrlError, "no host"),
        ("https://user:hunter2@cdn.example.com/clip.mp4", UnsafeUrlError, "credentials"),
        ("https://cdn.example.com:8443/clip.mp4", UnsafeUrlError, "port"),
        ("https://127.0.0.1/clip.mp4", UnsafeUrlError, "private, loopback"),
        ("https://10.0.0.5/clip.mp4", UnsafeUrlError, "private, loopback"),
        ("https://169.254.169.254/latest/meta-data", UnsafeUrlError, "private, loopback"),
        ("https://[::1]/clip.mp4", UnsafeUrlError, "private, loopback"),
        ("https://[::ffff:10.0.0.5]/clip.mp4", UnsafeUrlError, "private, loopback"),
        ("https://100.64.0.1/clip.mp4", UnsafeUrlError, "private, loopback"),
    ],
)
def test_unsupported_and_unsafe_inputs_are_refused_with_bounded_reasons(
    url: str, error: type[Exception], fragment: str
) -> None:
    with pytest.raises(error, match=fragment) as info:
        classify_url(url)
    assert "hunter2" not in str(info.value)


# --- destination gate --------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.1.1", "169.254.169.254",
        "100.64.0.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",
        "::1", "::", "fe80::1", "fc00::1", "fd12::1", "ff02::1", "::ffff:192.168.1.1",
        "2002:c0a8:0101::1", "64:ff9b::a00:1",
    ],
)
def test_non_public_addresses_are_recognized(address: str) -> None:
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "151.101.0.223", "2606:4700::6810:84e5"])
def test_public_addresses_pass(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is True


def _fake_getaddrinfo(answers: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in answers:
            raise socket.gaierror(8, "nodename nor servname provided")
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM, 6, "", (ip, port),
            )
            for ip in answers[host]
        ]

    return getaddrinfo


def test_resolver_requires_every_answer_to_be_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_ingest.socket,
        "getaddrinfo",
        _fake_getaddrinfo(
            {
                "cdn.example.com": ["151.101.0.223", "151.101.0.223", "2606:4700::6810:84e5"],
                "rebind.example.com": ["151.101.0.223", "10.0.0.5"],
                "internal.example.com": ["192.168.1.10"],
            }
        ),
    )
    assert resolve_public_host("cdn.example.com") == ["151.101.0.223", "2606:4700::6810:84e5"]
    with pytest.raises(UnsafeUrlError, match="non-public"):
        resolve_public_host("rebind.example.com")
    with pytest.raises(UnsafeUrlError, match="non-public"):
        resolve_public_host("internal.example.com")
    with pytest.raises(DownloadError, match="could not resolve"):
        resolve_public_host("nope.example.com")
    assert resolve_public_host("8.8.8.8") == ["8.8.8.8"]
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("[::1]")


# --- redaction --------------------------------------------------------------------


def test_redaction_strips_known_secrets_and_every_url_shape() -> None:
    raw = f"https://cdn.example.com/clip.mp4?{CANARY}"
    message = (
        f"HTTP Error 403 while fetching {raw}; retry https://r2---sn.googlevideo.com/x?sig=Q "
        f"or query {CANARY}; also see http://example.org/help"
    )
    cleaned = redact(message, raw, CANARY)
    assert CANARY not in cleaned
    assert "googlevideo" not in cleaned and "example.org" not in cleaned
    assert cleaned.count("<url>") == 4
    assert "HTTP Error 403" in cleaned


def test_bounded_title_drops_control_characters_and_caps_length() -> None:
    title = url_ingest.bounded_title("  A\x00 title\n with  \u200bzero width ")
    assert title == "A title with zero width"
    assert url_ingest.bounded_title("x" * 500) is not None
    assert len(url_ingest.bounded_title("x" * 500) or "") == url_ingest.TITLE_MAX_CHARS
    assert url_ingest.bounded_title(42) is None
    assert url_ingest.bounded_title("\x01\x02") is None


def test_download_cap_env_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_MAX_DOWNLOAD_BYTES", raising=False)
    assert url_ingest.max_download_bytes() == url_ingest.DEFAULT_MAX_DOWNLOAD_BYTES
    monkeypatch.setenv("TALKTHROUGH_MAX_DOWNLOAD_BYTES", "1048576")
    assert url_ingest.max_download_bytes() == 1_048_576
    for bad in ("0", "-5", "lots"):
        monkeypatch.setenv("TALKTHROUGH_MAX_DOWNLOAD_BYTES", bad)
        assert url_ingest.max_download_bytes() == url_ingest.DEFAULT_MAX_DOWNLOAD_BYTES


# --- URL index -----------------------------------------------------------------


def _store(manifest: Manifest) -> str:
    directory = jobs.job_dir(manifest.job_id)
    directory.mkdir(parents=True, exist_ok=True)
    save_manifest(manifest, directory)
    return manifest.job_id


def test_url_index_round_trips_without_storing_the_raw_url(isolated_home: Path) -> None:
    job_id = _store(make_manifest())
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    mapping = UrlMapping(
        job_id=job_id, provider=source.provider, created_at="2026-09-05T10:00:00+00:00",
        validators={"etag": '"abc"'},
    )
    path = url_ingest.save_mapping(source.mapping_key, mapping)
    assert path.parent == url_ingest.urls_root()
    assert CANARY not in path.name and "example" not in path.name
    stored = path.read_text(encoding="utf-8")
    assert CANARY not in stored and "clip.mp4" not in stored  # host is fine, the URL is not
    assert url_ingest.load_mapping(source.mapping_key) == mapping


def test_url_index_drops_mappings_to_deleted_jobs_lazily_and_in_gc(isolated_home: Path) -> None:
    job_id = _store(make_manifest())
    key = classify_url("https://youtu.be/nHfGfEiVdE8").mapping_key
    url_ingest.save_mapping(
        key, UrlMapping(job_id=job_id, provider="youtube", created_at="", provider_id="nHfGfEiVdE8")
    )
    other = classify_url("https://youtu.be/AAAAAAAAAAA").mapping_key
    url_ingest.save_mapping(other, UrlMapping(job_id="0" * 16, provider="youtube", created_at=""))
    assert url_ingest.load_mapping(other) is None
    assert not url_ingest.mapping_path(other).exists()
    assert url_ingest.load_mapping(key) is not None

    jobs.delete_job(job_id)
    assert url_ingest.sweep_stale_mappings() == [url_ingest.mapping_path(key).name]
    assert url_ingest.load_mapping(key) is None


def test_url_index_ignores_garbage_files(isolated_home: Path) -> None:
    key = classify_url("https://youtu.be/nHfGfEiVdE8").mapping_key
    path = url_ingest.mapping_path(key)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert url_ingest.load_mapping(key) is None
    assert not path.exists()
    path.write_text(json.dumps({"job_id": 7}), encoding="utf-8")
    assert url_ingest.load_mapping(key) is None


def test_url_lock_serializes_same_key_and_is_reentrant_across_keys(isolated_home: Path) -> None:
    with url_ingest.url_lock("youtube:a"), url_ingest.url_lock("youtube:b"):
        assert url_ingest.mapping_path("youtube:a").with_suffix(".lock").exists()


def test_url_lock_registry_does_not_retain_finished_keys(isolated_home: Path) -> None:
    import gc

    keys = {f"site:example:{index}" for index in range(25)}
    for key in keys:
        with url_ingest.url_lock(key):
            assert key in url_ingest._URL_LOCKS
    gc.collect()
    assert keys.isdisjoint(url_ingest._URL_LOCKS)


# --- managed source ---------------------------------------------------------------


def test_managed_source_names_are_talkthrough_owned() -> None:
    youtube = classify_url("https://youtu.be/nHfGfEiVdE8")
    assert url_ingest.managed_source_name(youtube, ".mp4") == "youtube-nHfGfEiVdE8.mp4"
    direct = classify_url(f"https://cdn.example.com/Evil Title.mp4?{CANARY}".replace(" ", "%20"))
    name = url_ingest.managed_source_name(direct, ".mp4")
    assert name.startswith("direct-") and name.endswith(".mp4")
    assert "Evil" not in name and CANARY not in name
    assert url_ingest.managed_source_relative(name) == f"source/{name}"


def test_install_managed_source_moves_into_the_job_dir(isolated_home: Path, tmp_path: Path) -> None:
    downloaded = tmp_path / "download.mp4"
    downloaded.write_bytes(b"media")
    target = url_ingest.install_managed_source("a" * 16, downloaded, "youtube-x.mp4")
    assert target == jobs.job_dir("a" * 16) / "source" / "youtube-x.mp4"
    assert target.read_bytes() == b"media"
    assert not downloaded.exists()
    resolved = url_ingest.source_path("/gone/original.mp4", "a" * 16, "source/youtube-x.mp4")
    assert resolved == target
    original = Path("/gone/original.mp4")
    assert url_ingest.source_path("/gone/original.mp4", "a" * 16, None) == original
    assert url_ingest.source_path("/gone/original.mp4", "a" * 16, "source/missing.mp4") == Path(
        "/gone/original.mp4"
    )


def test_download_workspace_is_private_and_removed(isolated_home: Path) -> None:
    with url_ingest.download_workspace() as staging:
        assert staging.parent == url_ingest.downloads_root()
        assert staging.name.startswith(".dl-")
        (staging / "download.mp4.part").write_bytes(b"x")
    assert not staging.exists()


def test_gc_sweeps_only_old_download_workspaces(isolated_home: Path) -> None:
    import os
    import time

    root = url_ingest.downloads_root()
    old = root / ".dl-old"
    fresh = root / ".dl-fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    stamp = time.time() - 3 * 86_400
    os.utime(old, (stamp, stamp))
    assert url_ingest.sweep_stale_downloads() == [".dl-old"]
    assert fresh.exists() and not old.exists()


# --- manifest origin ---------------------------------------------------------


def test_media_origin_round_trips_and_stays_absent_on_local_jobs(tmp_path: Path) -> None:
    manifest = make_manifest()
    before = json.dumps(manifest.to_dict())
    assert "origin" not in manifest.to_dict()["media"]
    assert "managed_source" not in manifest.to_dict()["media"]

    origin = MediaOrigin(
        kind="youtube", provider="youtube", url_sha256="ab" * 32, provider_id="nHfGfEiVdE8",
        host="www.youtube.com", title="Demo", published_at="2026-07-27",
        downloader="yt-dlp 2026.08.19", downloaded_bytes=1234,
        downloaded_at="2026-09-05T10:00:00+00:00",
    )
    manifest.media = replace(manifest.media, origin=origin, managed_source="source/youtube-x.mp4")
    payload = manifest.to_dict()["media"]
    assert payload["origin"]["provider_id"] == "nHfGfEiVdE8"
    assert payload["managed_source"] == "source/youtube-x.mp4"
    assert "host" in payload["origin"]
    save_manifest(manifest, tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded.media.origin == origin
    assert loaded.media.managed_source == "source/youtube-x.mp4"
    assert json.dumps(make_manifest().to_dict()) == before


def test_media_origin_tolerates_malformed_or_future_payloads() -> None:
    assert MediaOrigin.from_dict(None) is None
    assert MediaOrigin.from_dict({"kind": "youtube"}) is None
    parsed = MediaOrigin.from_dict(
        {
            "kind": "youtube", "provider": "youtube", "url_sha256": "ab" * 32,
            "downloaded_bytes": -1, "provider_id": 42, "future_field": "ignored",
        }
    )
    assert parsed is not None
    assert parsed.downloaded_bytes is None
    assert parsed.provider_id == "42"
    assert "future_field" not in parsed.to_dict()


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com:8o80/clip.mp4",
        "https://cdn.example.com:99999/clip.mp4",
        "https://[::1/clip.mp4",
    ],
)
def test_malformed_urls_are_input_errors_not_internal_errors(url: str) -> None:
    with pytest.raises(UnsupportedUrlError, match="malformed"):
        classify_url(url)
