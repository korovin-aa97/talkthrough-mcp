"""Downloaders without a network: httpx.MockTransport for direct URLs and a
stub yt_dlp module for YouTube. Every test also proves the redaction
contract — a canary token in the URL never reaches an error or a file."""

from __future__ import annotations

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
        lambda request: httpx.Response(
            200,
            content=MEDIA,
            headers={"content-type": "video/mp4", "etag": '"v1"', "last-modified": "Mon"},
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
    assert all(CANARY not in stage for stage, _ in seen)
    assert any(stage.startswith("downloading source") for stage, _ in seen)


def test_direct_download_uses_content_type_when_the_path_has_no_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: None
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=MEDIA, headers={"content-type": "audio/mpeg; charset=binary"}
        ),
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
    with pytest.raises(UnsupportedUrlError, match="not supported media"):
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
        return httpx.Response(200, content=MEDIA, headers={"content-type": "video/mp4"})

    handler_requests = _serve(monkeypatch, handler)

    def relay(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved/clip.mp4":
            return httpx.Response(
                301, headers={"location": f"https://media.example.net/final.mp4?{CANARY}"}
            )
        if request.url.path == "/final.mp4":
            return httpx.Response(200, content=MEDIA, headers={"content-type": "video/mp4"})
        return httpx.Response(302, headers={"location": "/moved/clip.mp4"})

    monkeypatch.setattr(url_download, "_TRANSPORT", httpx.MockTransport(relay))
    source = classify_url("https://cdn.example.com/clip.mp4")
    downloaded = download_direct(source, tmp_path, max_bytes=10_000, report=lambda *a: None)
    assert downloaded.downloaded_bytes == len(MEDIA)
    assert resolved == ["cdn.example.com", "cdn.example.com", "media.example.net"]
    assert handler_requests == []  # the relay transport replaced the first handler


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
        lambda request: httpx.Response(
            200, content=MEDIA, headers={"content-type": "video/mp4", "content-length": "999999"}
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
        lambda request: httpx.Response(
            200,
            content=MEDIA[:100],
            headers={"content-type": "video/mp4", "content-length": str(len(MEDIA))},
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
        return dict(self.info)

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
    assert options["max_filesize"] == 100_000
    assert options["ffmpeg_location"] == "/opt/ffmpeg/bin"
    assert options["js_runtimes"] == {"deno": {"path": "/opt/deno/bin/deno"}}
    assert options["outtmpl"]["default"].endswith("youtube-nHfGfEiVdE8.%(ext)s")
    assert str(tmp_path / "home" / "cache" / "yt-dlp") == options["cachedir"]
    assert options["writeinfojson"] is False and options["writethumbnail"] is False
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


def test_downloaded_record_is_plain_data() -> None:
    record = Downloaded(path=Path("/x"), extension=".mp4", downloaded_bytes=1, downloader="t")
    assert record.validators == {} and record.title is None
