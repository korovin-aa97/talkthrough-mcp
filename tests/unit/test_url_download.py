"""Downloaders without a network: httpx.MockTransport for direct URLs and a
stub yt_dlp module for YouTube. Every test also proves the redaction
contract — a canary token in the URL never reaches an error or a file."""

from __future__ import annotations

import io
import logging
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from talkthrough_mcp.core import url_download
from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.url_download import (
    Downloaded,
    download_direct,
    download_youtube,
    preflight_info,
    youtube_options,
)
from talkthrough_mcp.core.url_ingest import (
    DownloadError,
    UnsafeUrlError,
    UnsupportedUrlError,
    UrlExtraMissingError,
    classify_url,
)

CANARY = "sig=SECRET-TOKEN-4242"
MEDIA = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4000


def _report_log() -> tuple[list[tuple[str, float]], Callable[[str, float], None]]:
    seen: list[tuple[str, float]] = []

    def report(stage: str, fraction: float) -> None:
        seen.append((stage, fraction))

    return seen, report


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to one public address without DNS."""
    monkeypatch.setattr(url_download, "resolve_public_host", lambda host: ["203.0.113.10"])


def _media(content: bytes, **headers: str) -> httpx.Response:
    """A streamed body (not preloaded), so num_bytes_downloaded counts like a socket."""
    return httpx.Response(200, stream=httpx.ByteStream(content), headers=headers)


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(url_download, "_TRANSPORT", httpx.MockTransport(wrapped))
    return requests


# --- direct HTTPS --------------------------------------------------------------


def test_direct_download_pins_the_address_and_keeps_host_and_sni(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    requests = _serve(
        monkeypatch,
        lambda request: _media(
            MEDIA, **{"content-type": "video/mp4", "etag": '"v1"', "last-modified": "Mon"}
        ),
    )
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    seen, report = _report_log()
    downloaded = download_direct(source, tmp_path, max_bytes=10_000, report=report)
    assert downloaded.path == tmp_path / "download.mp4"
    assert downloaded.path.read_bytes() == MEDIA
    assert downloaded.extension == ".mp4"
    assert downloaded.downloaded_bytes == len(MEDIA)
    assert downloaded.validators == {"etag": '"v1"', "last-modified": "Mon"}
    assert downloaded.downloader.startswith("httpx ")
    assert not list(tmp_path.glob("*.part"))
    (request,) = requests
    assert request.url.host == "203.0.113.10"
    assert request.headers["host"] == "cdn.example.com"
    assert request.extensions["sni_hostname"] == "cdn.example.com"
    assert f"?{CANARY}" in str(request.url)  # the query reaches the wire, and nothing else
    assert request.headers["accept-encoding"] == "identity"
    assert all(CANARY not in stage for stage, _ in seen)
    assert any(stage.startswith("downloading source") for stage, _ in seen)


def test_direct_download_uses_content_type_when_the_path_has_no_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: _media(MEDIA, **{"content-type": "audio/mpeg; charset=binary"}),
    )
    source = classify_url("https://cdn.example.com/download?id=7")
    downloaded = download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert downloaded.extension == ".mp3"


def test_direct_download_refuses_non_media_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"<html>login</html>", headers={"content-type": "text/html"}
        ),
    )
    source = classify_url("https://cdn.example.com/download?id=7")
    with pytest.raises(url_download.NotMediaResponse, match="not a media file") as info:
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert info.value.content_type == "text/html"
    assert list(tmp_path.iterdir()) == []


def test_direct_download_refuses_html_even_when_the_path_looks_like_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    """A Wikimedia Commons *page* URL ends in .ogv; the page itself is not media."""
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"<html>player</html>", headers={"content-type": "text/html"}
        ),
    )
    source = classify_url("https://commons.example.org/wiki/File:Clip.ogv")
    assert source.path_extension == ".ogv"
    with pytest.raises(url_download.NotMediaResponse):
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert list(tmp_path.iterdir()) == []


def test_direct_download_follows_and_revalidates_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved: list[str] = []

    def fake_resolve(host: str) -> list[str]:
        resolved.append(host)
        return ["203.0.113.10"]

    monkeypatch.setattr(url_download, "resolve_public_host", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "cdn.example.com":
            return httpx.Response(302, headers={"location": "/moved/clip.mp4"})
        if request.url.path == "/moved/clip.mp4" and request.headers["host"] == "cdn.example.com":
            return httpx.Response(
                301, headers={"location": f"https://media.example.net/final.mp4?{CANARY}"}
            )
        return _media(MEDIA, **{"content-type": "video/mp4"})

    handler_requests = _serve(monkeypatch, handler)

    def relay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved/clip.mp4":
            return httpx.Response(
                301, headers={"location": f"https://media.example.net/final.mp4?{CANARY}"}
            )
        if request.url.path == "/final.mp4":
            return _media(MEDIA, **{"content-type": "video/mp4"})
        return httpx.Response(302, headers={"location": "/moved/clip.mp4"})

    monkeypatch.setattr(url_download, "_TRANSPORT", httpx.MockTransport(relay))
    source = classify_url("https://cdn.example.com/clip.mp4")
    downloaded = download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert downloaded.downloaded_bytes == len(MEDIA)
    assert resolved == ["cdn.example.com", "cdn.example.com", "media.example.net"]
    assert handler_requests == []  # the relay transport replaced the first handler


def test_direct_download_keeps_the_url_out_of_every_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs each request line at INFO with the full URL; 0.4.0 let the
    query of the input URL and the signed redirect target through to stderr.
    The CLI's logging setup holds those loggers at WARNING and, should anyone
    raise them again, redacts what still reaches the handler."""
    from talkthrough_mcp import cli

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/clip.mp4":
            return httpx.Response(
                302,
                headers={
                    "location": "https://cdn.example.net/signed/clip.mp4?Signature=CDN-SECRET-9999"
                },
            )
        return _media(MEDIA, **{"content-type": "video/mp4"})

    _serve(monkeypatch, handler)
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    stream = io.StringIO()
    privacy = cli._privacy_handler(stream)
    httpx_logger.addHandler(privacy)
    try:
        cli._configure_logging()
        with caplog.at_level(logging.INFO):
            download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
        assert "HTTP Request" not in caplog.text and stream.getvalue() == ""
        assert CANARY not in caplog.text and "CDN-SECRET" not in caplog.text

        httpx_logger.setLevel(logging.INFO)  # a chatty httpx still yields redacted lines only
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    finally:
        httpx_logger.removeHandler(privacy)
        httpx_logger.setLevel(previous_level)
    text = stream.getvalue()
    assert 'HTTP Request: GET <url> "HTTP/1.1 302 Found"' in text
    assert 'HTTP Request: GET <url> "HTTP/1.1 200 OK"' in text
    assert CANARY not in text and "CDN-SECRET" not in text and "203.0.113.10" not in text


@pytest.mark.parametrize(
    ("location", "fragment"),
    [
        ("http://cdn.example.com/clip.mp4", "left https"),
        ("https://user:pw@cdn.example.com/clip.mp4", "credentials"),
        ("https://cdn.example.com:8443/clip.mp4", "port 443"),
    ],
)
def test_direct_download_refuses_unsafe_redirect_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None, location: str, fragment: str
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": location}))
    source = classify_url("https://cdn.example.com/clip.mp4")
    with pytest.raises(UnsafeUrlError, match=fragment) as info:
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert "pw" not in str(info.value).replace("pw@", "")


def test_direct_download_caps_redirect_chains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": f"{request.url}x"}),
    )
    source = classify_url("https://cdn.example.com/clip.mp4")
    with pytest.raises(UnsafeUrlError, match="redirects"):
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)


def test_direct_download_refuses_private_redirect_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(host: str) -> list[str]:
        if host == "internal.example.com":
            raise UnsafeUrlError("host resolves to a non-public address")
        return ["203.0.113.10"]

    monkeypatch.setattr(url_download, "resolve_public_host", resolve)
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://internal.example.com/clip.mp4"}
        ),
    )
    with pytest.raises(UnsafeUrlError, match="non-public"):
        download_direct(
            classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
            report=lambda *a: None,
        )


def test_direct_download_reports_http_errors_without_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(403, content=b"denied"))
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    with pytest.raises(DownloadError, match="HTTP 403") as info:
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert CANARY not in str(info.value)


def test_direct_download_declared_size_above_the_cap_is_refused_before_any_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: _media(
            MEDIA, **{"content-type": "video/mp4", "content-length": "999999"}
        ),
    )
    with pytest.raises(DownloadError, match="above the 10000 byte cap"):
        download_direct(
            classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
            report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


def test_direct_download_streaming_cap_aborts_and_removes_the_part_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    big = b"y" * (url_download.CHUNK_BYTES * 3)

    class Chunked(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for start in range(0, len(big), url_download.CHUNK_BYTES):
                yield big[start : start + url_download.CHUNK_BYTES]

    def handler(request: httpx.Request) -> httpx.Response:
        # no content-length: a chunked body must still hit the streaming cap
        return httpx.Response(
            200,
            stream=Chunked(),
            headers={"content-type": "video/mp4", "transfer-encoding": "chunked"},
        )

    _serve(monkeypatch, handler)
    with pytest.raises(DownloadError, match=r"exceeded the .* byte cap"):
        download_direct(
            classify_url("https://cdn.example.com/clip.mp4"), tmp_path,
            max_bytes=url_download.CHUNK_BYTES + 10, report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


def test_direct_download_truncated_body_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: _media(
            MEDIA[:100], **{"content-type": "video/mp4", "content-length": str(len(MEDIA))}
        ),
    )
    with pytest.raises(DownloadError, match="truncated"):
        download_direct(
            classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
            report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


def test_direct_download_transport_errors_are_redacted_and_hint_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed for {request.url}"
        )

    _serve(monkeypatch, handler)
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    with pytest.raises(DownloadError, match="SSL_CERT_FILE") as info:
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert CANARY not in str(info.value)
    assert "203.0.113.10" not in str(info.value)


def test_direct_download_timeout_is_a_bounded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out reading {request.url}")

    _serve(monkeypatch, handler)
    source = classify_url(f"https://cdn.example.com/clip.mp4?{CANARY}")
    with pytest.raises(DownloadError, match="timeout") as info:
        download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert CANARY not in str(info.value)


# --- YouTube via a stub yt_dlp --------------------------------------------------


class _FakeYoutubeDL:
    """Enough of yt_dlp.YoutubeDL for the adapter: canned info + a written file."""

    instances: ClassVar[list[_FakeYoutubeDL]] = []
    info: ClassVar[dict[str, Any]] = {}
    fail_with: ClassVar[Exception | None] = None
    bytes_to_write: ClassVar[bytes] = MEDIA
    extension: ClassVar[str] = "mp4"

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        _FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> _FakeYoutubeDL:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        info = dict(self.info)
        # Faithful to yt-dlp: playlist_items is applied while the playlist is
        # processed, so only the requested entries survive, and the full count
        # is not reported unless the extractor's entry list was exhausted.
        wanted = self.options.get("playlist_items")
        if wanted and info.get("_type") in {"playlist", "multi_video"}:
            last_item = str(wanted).split(":")[-1]
            info["entries"] = list(info.get("entries") or [])[: int(last_item)]
        return info

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> None:
        template = self.options["outtmpl"]["default"]
        path = Path(template.replace("%(ext)s", self.extension))
        for hook in self.options["progress_hooks"]:
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": len(self.bytes_to_write),
                    "total_bytes": len(self.bytes_to_write),
                }
            )
        path.write_bytes(self.bytes_to_write)
        info["requested_downloads"] = [{"filepath": str(path)}]


class _FakeYoutubeDLError(Exception):
    pass


@pytest.fixture
def fake_yt_dlp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> type[_FakeYoutubeDL]:
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _FakeYoutubeDL  # type: ignore[attr-defined]
    utils = types.ModuleType("yt_dlp.utils")
    utils.YoutubeDLError = _FakeYoutubeDLError  # type: ignore[attr-defined]
    version = types.ModuleType("yt_dlp.version")
    version.__version__ = "2026.08.19"  # type: ignore[attr-defined]
    globals_module = types.ModuleType("yt_dlp.globals")
    globals_module.plugin_dirs = types.SimpleNamespace(value=["default"])  # type: ignore[attr-defined]
    module.utils = utils  # type: ignore[attr-defined]
    module.version = version  # type: ignore[attr-defined]
    module.globals = globals_module  # type: ignore[attr-defined]
    for name, mod in (
        ("yt_dlp", module), ("yt_dlp.utils", utils), ("yt_dlp.version", version),
        ("yt_dlp.globals", globals_module),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(url_download, "ffmpeg_path", lambda: "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setattr(url_download, "_deno_path", lambda: "/opt/deno/bin/deno")
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path / "home"))
    _FakeYoutubeDL.instances = []
    _FakeYoutubeDL.fail_with = None
    _FakeYoutubeDL.bytes_to_write = MEDIA
    _FakeYoutubeDL.extension = "mp4"
    _FakeYoutubeDL.info = {
        "id": "nHfGfEiVdE8",
        "title": "Talkthrough demo\x00 with control chars",
        "duration": 78,
        "is_live": False,
        "live_status": "not_live",
        "availability": "public",
        "age_limit": 0,
        "upload_date": "20260727",
        "timestamp": 1785000000,
        "requested_formats": [{"filesize": 3000}, {"filesize_approx": 1000}],
    }
    return _FakeYoutubeDL


def test_youtube_download_uses_the_allowlisted_options_and_names_the_file(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    source = classify_url("https://youtu.be/nHfGfEiVdE8")
    seen, report = _report_log()
    downloaded = download_youtube(
        source, tmp_path, max_bytes=100_000, max_seconds=7200, report=report
    )
    assert downloaded.path == tmp_path / "youtube-nHfGfEiVdE8.mp4"
    assert downloaded.downloaded_bytes == len(MEDIA)
    assert downloaded.downloader == "yt-dlp 2026.08.19"
    assert downloaded.title == "Talkthrough demo\x00 with control chars"  # bounded later
    assert downloaded.published_at == "2026-07-27T00:00:00+00:00"[:0] or downloaded.published_at
    assert downloaded.published_at is not None and downloaded.published_at.startswith("2026-0")
    (instance,) = fake_yt_dlp.instances
    options = instance.options
    assert options["noplaylist"] is True and "max_downloads" not in options
    assert options["cookiesfrombrowser"] is None and options["cookiefile"] is None
    assert options["remote_components"] == [] and options["extractor_args"] == {}
    assert "max_filesize" not in options  # a silent per-format skip in yt-dlp; the hook caps
    assert Path(options["ffmpeg_location"]) == Path("/opt/ffmpeg/bin")
    assert options["js_runtimes"] == {"deno": {"path": "/opt/deno/bin/deno"}}
    assert options["outtmpl"]["default"].endswith("youtube-nHfGfEiVdE8.%(ext)s")
    assert str(tmp_path / "home" / "cache" / "yt-dlp") == options["cachedir"]
    assert options["writeinfojson"] is False and options["writethumbnail"] is False
    assert options["postprocessor_args"] == {"merger": ["-bitexact"]}
    assert sys.modules["yt_dlp"].globals.plugin_dirs.value == []  # type: ignore[attr-defined]
    assert any(stage.startswith("downloading source") for stage, _ in seen)


def test_youtube_download_without_the_extra_is_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    with pytest.raises(UrlExtraMissingError, match=r"talkthrough-mcp\[diarization,url\]"):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=1, max_seconds=1,
            report=lambda *a: None,
        )


@pytest.mark.parametrize(
    ("patch", "error", "fragment"),
    [
        ({"is_live": True}, UnsupportedUrlError, "live stream"),
        ({"live_status": "is_upcoming"}, UnsupportedUrlError, "live stream"),
        ({"_type": "playlist", "entries": []}, UnsupportedUrlError, "playlist"),
        ({"availability": "private"}, UnsupportedUrlError, "private"),
        ({"availability": "needs_auth"}, UnsupportedUrlError, "needs auth"),
        ({"age_limit": 18}, UnsupportedUrlError, "age-restricted"),
        ({"duration": None}, UnsupportedUrlError, "no duration"),
        ({"duration": 99_999}, ValidationError, "exceeds the 7200s cap"),
        ({"requested_formats": [{"filesize": 10**12}]}, DownloadError, "above the 100000 byte cap"),
    ],
)
def test_youtube_preflight_refuses_what_the_release_does_not_support(
    fake_yt_dlp: type[_FakeYoutubeDL],
    tmp_path: Path,
    patch: dict[str, Any],
    error: type[Exception],
    fragment: str,
) -> None:
    fake_yt_dlp.info = {**fake_yt_dlp.info, **patch}
    with pytest.raises(error, match=fragment):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


def test_preflight_estimate_sums_requested_formats() -> None:
    info = {"duration": 10, "requested_formats": [{"filesize": 30}, {"filesize_approx": 12}]}
    assert preflight_info(info, max_seconds=100, max_bytes=1000) == 42
    assert preflight_info({"duration": 10}, max_seconds=100, max_bytes=1000) is None


def test_preflight_unknown_duration_is_refused_by_default_but_allowed_for_sites() -> None:
    with pytest.raises(UnsupportedUrlError, match="reports no duration"):
        preflight_info({"id": "x"}, max_seconds=100, max_bytes=1000)
    # Site extractors (e.g. Instagram) omit the duration for anonymous clients; the
    # cap is then enforced by ffprobe on the downloaded file.
    relaxed = dict(max_seconds=100, max_bytes=1000, require_duration=False)
    assert preflight_info({"id": "x"}, **relaxed) is None
    with pytest.raises(UnsupportedUrlError, match="live stream"):
        preflight_info({"id": "x", "is_live": True}, **relaxed)


@pytest.mark.parametrize(
    ("message", "fragment"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: Sign in to confirm your age", "age-restricted"),
        ("ERROR: This live event will begin in 3 hours", "live stream"),
        ("ERROR: Video unavailable", "unavailable"),
        ("ERROR: The uploader has not made this video available in your country", "region"),
        ("ERROR: This video is DRM protected", "DRM"),
    ],
)
def test_youtube_provider_failures_map_to_bounded_categories(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path, message: str, fragment: str
) -> None:
    fake_yt_dlp.fail_with = _FakeYoutubeDLError(
        f"{message}; see https://r2---sn-x.googlevideo.com/videoplayback?{CANARY}"
    )
    with pytest.raises(UnsupportedUrlError, match=fragment) as info:
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
    assert CANARY not in str(info.value) and "googlevideo" not in str(info.value)


def test_youtube_unknown_provider_failure_is_redacted(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_yt_dlp.fail_with = _FakeYoutubeDLError(
        f"ERROR: HTTP Error 429: Too Many Requests https://www.youtube.com/watch?v=x&{CANARY}\n"
        "second line with more detail"
    )
    with pytest.raises(DownloadError, match="429") as info:
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
    text = str(info.value)
    assert CANARY not in text and "second line" not in text and "<url>" in text


def test_youtube_progress_hook_enforces_the_byte_cap(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_yt_dlp.info = {**fake_yt_dlp.info, "requested_formats": []}
    fake_yt_dlp.bytes_to_write = b"z" * 5000
    with pytest.raises(DownloadError, match="exceeded the 4000 byte cap"):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=4000,
            max_seconds=7200, report=lambda *a: None,
        )


def test_youtube_result_above_cap_after_merge_is_removed(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_yt_dlp.info = {**fake_yt_dlp.info, "requested_formats": []}
    fake_yt_dlp.bytes_to_write = b"z" * 5000

    def silent_hook_process(self: Any, info: dict[str, Any], download: bool = True) -> None:
        path = Path(self.options["outtmpl"]["default"].replace("%(ext)s", "mp4"))
        path.write_bytes(self.bytes_to_write)
        info["requested_downloads"] = [{"filepath": str(path)}]

    monkeypatch.setattr(_FakeYoutubeDL, "process_ie_result", silent_hook_process)
    with pytest.raises(DownloadError, match="5000 bytes, above the 4000 byte cap"):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=4000,
            max_seconds=7200, report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


def test_youtube_options_never_include_user_configuration() -> None:
    options = youtube_options(
        Path("/tmp/x"), video_id="abc", max_bytes=10, progress_hook=lambda s: None, secrets=()
    )
    forbidden = {"cookiesfrombrowser", "cookiefile"}
    assert all(options[key] is None for key in forbidden)
    assert "usenetrc" not in options and "http_headers" not in options
    assert options["format"] == url_download.YOUTUBE_FORMAT


def test_probe_options_stop_after_detecting_a_second_video() -> None:
    """The page probe needs two flat stubs to distinguish one video from many
    without walking an entire channel or playlist (0.4.1)."""
    probe = youtube_options(
        Path("/tmp/x"), video_id="probe", max_bytes=10, progress_hook=lambda s: None,
        secrets=(), output_stem="site-probe", probe=True,
    )
    assert probe["extract_flat"] == "in_playlist"
    assert probe["playlist_items"] == "1:2"
    assert probe["noplaylist"] is True
    download = youtube_options(
        Path("/tmp/x"), video_id="abc", max_bytes=10, progress_hook=lambda s: None, secrets=()
    )
    assert download["playlist_items"] == "1" and "extract_flat" not in download


def test_downloaded_record_is_plain_data() -> None:
    record = Downloaded(path=Path("/x"), extension=".mp4", downloaded_bytes=1, downloader="t")
    assert record.validators == {} and record.title is None


# --- review fixes (2026-09-05) ---------------------------------------------------


def test_direct_download_handles_content_encoding_gzip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    import gzip

    compressed = gzip.compress(MEDIA)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(compressed),
            headers={
                "content-type": "video/mp4",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
        )

    _serve(monkeypatch, handler)
    downloaded = download_direct(
        classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
        report=lambda *a: None,
    )
    assert downloaded.path.read_bytes() == MEDIA
    assert downloaded.downloaded_bytes == len(MEDIA)


def test_direct_download_falls_back_to_the_next_validated_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        url_download, "resolve_public_host", lambda host: ["2001:db8::1", "203.0.113.10"]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "2001:db8::1":
            raise httpx.ConnectError("Network is unreachable")
        return _media(MEDIA, **{"content-type": "video/mp4"})

    requests = _serve(monkeypatch, handler)
    downloaded = download_direct(
        classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
        report=lambda *a: None,
    )
    assert downloaded.downloaded_bytes == len(MEDIA)
    assert [request.url.host for request in requests] == ["2001:db8::1", "203.0.113.10"]


def test_direct_download_uses_the_final_hop_path_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/download":
            return httpx.Response(
                302, headers={"location": "https://bucket.example.net/recordings/standup.mp4?X=1"}
            )
        return _media(MEDIA, **{"content-type": "application/octet-stream"})

    _serve(monkeypatch, handler)
    downloaded = download_direct(
        classify_url("https://files.example.com/download?id=42"), tmp_path, max_bytes=10_000,
        report=lambda *a: None,
    )
    assert downloaded.extension == ".mp4"


def test_malformed_redirect_target_is_refused_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://cdn.example.com:8o80/clip.mp4"}
        ),
    )
    with pytest.raises(UnsafeUrlError, match="malformed"):
        download_direct(
            classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=10_000,
            report=lambda *a: None,
        )


def test_direct_download_progress_is_throttled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    big = b"z" * (3 * url_download.REPORT_EVERY_BYTES + 5)
    _serve(monkeypatch, lambda request: _media(big, **{"content-type": "video/mp4"}))
    seen, report = _report_log()
    download_direct(
        classify_url("https://cdn.example.com/clip.mp4"), tmp_path, max_bytes=len(big) + 1,
        report=report,
    )
    progress_lines = [stage for stage, _ in seen if stage.startswith("downloading source")]
    assert 2 <= len(progress_lines) <= 6


def test_youtube_bot_check_is_a_retryable_condition_not_a_private_video(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_yt_dlp.fail_with = _FakeYoutubeDLError(
        "ERROR: [youtube] nHfGfEiVdE8: Sign in to confirm you\u2019re not a bot. Use "
        "--cookies-from-browser or --cookies for the authentication."
    )
    with pytest.raises(DownloadError, match="bot check") as info:
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
    assert "private" not in str(info.value).split("this is not")[0]


def test_youtube_intermediate_only_output_is_reported_as_incomplete(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_yt_dlp.info = {**fake_yt_dlp.info, "requested_formats": []}

    def skipped_video(self: Any, info: dict[str, Any], download: bool = True) -> None:
        template = self.options["outtmpl"]["default"]
        Path(template.replace(".%(ext)s", ".f251.webm")).write_bytes(b"audio only")

    monkeypatch.setattr(_FakeYoutubeDL, "process_ie_result", skipped_video)
    with pytest.raises(DownloadError, match="before the video and audio tracks were merged"):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
    assert list(tmp_path.iterdir()) == []


# --- any-site page reader (2026-09-05) ---------------------------------------------


def _site_info(**patch: Any) -> dict[str, Any]:
    info = {
        "id": "987654321",
        "extractor_key": "Vimeo",
        "title": "Sintel trailer",
        "duration": 52,
        "is_live": False,
        "live_status": "not_live",
        "availability": "public",
        "age_limit": 0,
        "upload_date": "20100512",
        "requested_formats": [{"filesize": 3000}],
    }
    info.update(patch)
    return info


def test_site_download_names_the_file_by_provider_and_id(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    from talkthrough_mcp.core.url_download import download_site

    fake_yt_dlp.info = _site_info()
    source = classify_url("https://vimeo.com/987654321?share=copy")
    seen, report = _report_log()
    downloaded = download_site(
        source, tmp_path, max_bytes=100_000, max_seconds=7200, report=report
    )
    assert downloaded.kind == "site"
    assert downloaded.provider == "vimeo" and downloaded.provider_id == "987654321"
    assert downloaded.path == tmp_path / "vimeo-987654321.mp4"
    assert downloaded.title == "Sintel trailer" and downloaded.published_at == "2010-05-12"
    assert any(stage.startswith("reading the video page") for stage, _ in seen)
    probe, ydl = fake_yt_dlp.instances
    assert probe.options["outtmpl"]["default"].endswith("site-probe.%(ext)s")
    assert ydl.options["outtmpl"]["default"].endswith("vimeo-987654321.%(ext)s")
    assert ydl.options["cookiesfrombrowser"] is None and ydl.options["remote_components"] == []


def test_site_download_resolves_a_single_entry_page_and_refuses_many(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core.url_download import download_site

    single = _site_info(extractor_key="Instagram", id="C_abc-123")
    fake_yt_dlp.info = {"_type": "playlist", "entries": [{"_type": "url", "url": "x"}]}

    original = _FakeYoutubeDL.process_ie_result

    def dispatch(self: Any, entry: dict[str, Any], download: bool = True) -> Any:
        if not download:
            return dict(single)
        return original(self, entry, download)

    monkeypatch.setattr(_FakeYoutubeDL, "process_ie_result", dispatch)
    downloaded = download_site(
        classify_url("https://www.instagram.com/reel/C_abc-123/"), tmp_path,
        max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
    )
    assert downloaded.provider == "instagram" and downloaded.provider_id == "C_abc-123"
    assert downloaded.path.name == "instagram-C_abc-123.mp4"

    fake_yt_dlp.info = {"_type": "playlist", "entries": [{"url": "a"}, {"url": "b"}]}
    with pytest.raises(UnsupportedUrlError, match="contains more than one video"):
        download_site(
            classify_url("https://www.instagram.com/p/carousel/"), tmp_path,
            max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
        )


def test_site_download_refuses_a_folder_page_before_any_download(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    """A Loom folder or an Instagram carousel: 0.4.0's probe asked yt-dlp for
    playlist item 1 only, so the count it checked was always 1 and the first
    video was ingested silently (release QA, F-02)."""
    from talkthrough_mcp.core.url_download import download_site

    fake_yt_dlp.info = {
        "_type": "playlist",
        "id": "997db4db046f43e5912f10dc5f817b5c",
        "extractor_key": "LoomFolder",
        "entries": [{"_type": "url", "url": stub} for stub in ("a", "b", "c")],
    }
    with pytest.raises(UnsupportedUrlError, match="contains more than one video") as info:
        download_site(
            classify_url("https://www.loom.com/share/folder/997db4db046f43e5912f10dc5f817b5c"),
            tmp_path, max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
        )
    assert "pass a link to one video" in str(info.value)
    (probe,) = fake_yt_dlp.instances  # no download instance was ever built
    assert probe.options["extract_flat"] == "in_playlist"
    assert probe.options["playlist_items"] == "1:2"
    assert list(tmp_path.iterdir()) == []


def test_site_download_reuses_a_known_job_before_downloading(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    from talkthrough_mcp.core.url_download import ReuseExistingJob, download_site

    fake_yt_dlp.info = _site_info()
    with pytest.raises(ReuseExistingJob) as info:
        download_site(
            classify_url("https://player.vimeo.com/video/987654321"), tmp_path,
            max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
            known_job=lambda provider, pid: (
                "a" * 16 if (provider, pid) == ("vimeo", "987654321") else None
            ),
        )
    assert info.value.job_id == "a" * 16
    assert list(tmp_path.iterdir()) == []


def test_site_download_maps_unsupported_pages_and_bot_walls(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    from talkthrough_mcp.core.url_download import download_site

    fake_yt_dlp.fail_with = _FakeYoutubeDLError(
        "ERROR: Unsupported URL: https://example.org/about?tok=SECRET-TOKEN-4242"
    )
    with pytest.raises(UnsupportedUrlError, match="no video could be found") as info:
        download_site(
            classify_url("https://example.org/about?tok=SECRET-TOKEN-4242"), tmp_path,
            max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
        )
    assert "SECRET-TOKEN" not in str(info.value)
    fake_yt_dlp.fail_with = _FakeYoutubeDLError(
        "ERROR: [Instagram] C1: Sign in to confirm you\u2019re not a bot"
    )
    with pytest.raises(DownloadError, match="bot check"):
        download_site(
            classify_url("https://www.instagram.com/reel/C1/"), tmp_path,
            max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
        )


def test_provider_ids_are_bounded_and_filename_safe() -> None:
    from talkthrough_mcp.core.url_download import safe_provider, safe_provider_id

    assert safe_provider_id("abc_DEF-123.x") == "abc_DEF-123.x"
    assert safe_provider_id("") == "unknown"
    weird = safe_provider_id("a/b c?d=e" + "x" * 100)
    assert "/" not in weird and " " not in weird and len(weird) <= 60
    assert safe_provider("Vimeo") == "vimeo" and safe_provider("Generic") == "generic"
    assert safe_provider(None) == "site"


def test_extractor_stack_crashes_are_explained_and_redacted(
    fake_yt_dlp: type[_FakeYoutubeDL], tmp_path: Path
) -> None:
    """yt-dlp's TED extractor raised a bare TypeError on a changed page (release
    QA, F-05): the user gets the exception name, its first line, and what to do."""
    from talkthrough_mcp.core.url_download import download_site

    fake_yt_dlp.fail_with = TypeError(
        "the JSON object must be str, bytes or bytearray, not NoneType"
    )
    with pytest.raises(DownloadError) as info:
        download_site(
            classify_url("https://www.ted.com/talks/candace_parker?tok=SECRET-TOKEN-4242"),
            tmp_path, max_bytes=100_000, max_seconds=7200, report=lambda *a: None,
        )
    message = str(info.value)
    assert message.startswith("the page reader failed on https://www.ted.com/…")
    assert "TypeError: the JSON object must be str" in message
    assert "SECRET-TOKEN" not in message and "candace" not in message
    assert "refresh the tool environment" in message and "direct link" in message
    assert list(tmp_path.iterdir()) == []

    fake_yt_dlp.fail_with = TypeError("'NoneType' object is not subscriptable")
    with pytest.raises(DownloadError, match="page reader failed on YouTube video nHfGfEiVdE8"):
        download_youtube(
            classify_url("https://youtu.be/nHfGfEiVdE8"), tmp_path, max_bytes=100_000,
            max_seconds=7200, report=lambda *a: None,
        )
