"""The two source downloaders behind ``process_url``.

``download_direct`` fetches one public HTTPS media file with httpx: every
hop is resolved and checked by :mod:`url_ingest`'s destination gate, the
connection is pinned to the checked address (SNI and ``Host`` carry the
name), redirects are followed by hand and re-validated, and the body is
streamed under a hard byte cap into a private ``.part`` file.

``download_youtube`` drives ``yt_dlp.YoutubeDL`` — the optional ``[url]``
extra — with an allowlisted option set: no user config, no plugins, no
cookies, one video, no live streams, a duration cap before the download
and a byte cap enforced from the progress hook while it runs. Output names
are Talkthrough's; the remote title never touches the filesystem.

Both return a :class:`Downloaded` record; the caller verifies the bytes with
ffprobe before anything reaches the job store.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .. import __version__
from .errors import ValidationError
from .ffmpeg import ffmpeg_path
from .url_ingest import (
    CONTENT_TYPE_EXTENSIONS,
    MAX_REDIRECTS,
    MEDIA_EXTENSIONS,
    DownloadError,
    UnsafeUrlError,
    UnsupportedUrlError,
    UrlExtraMissingError,
    UrlSource,
    _bounded_reason,
    check_free_disk,
    redact,
    resolve_public_host,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

CHUNK_BYTES = 64 * 1024
DISK_CHECK_EVERY_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES_UNKNOWN_SIZE = 512 * 1024 * 1024
CONNECT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 60.0
DOWNLOAD_DEADLINE_S = 3600.0
# Keyframe OCR gains nothing above 1080p; the cap keeps YouTube downloads and
# the frame extraction pass bounded without promising an exact container.
YOUTUBE_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
YOUTUBE_MAX_HEIGHT = 1080
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Test seam: an httpx transport that replaces real sockets (never set in production).
_TRANSPORT: Any = None
_MISSING_EXTRA = (
    "YouTube ingestion needs the optional [url] extra (yt-dlp) — install the server as "
    'uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization,url]" (JSON configs: '
    '"args": ["--python", ">=3.11,<3.14", "talkthrough-mcp[diarization,url]"]), '
    "restart the client and retry"
)


@dataclass(frozen=True)
class Downloaded:
    path: Path
    extension: str
    downloaded_bytes: int
    downloader: str
    title: str | None = None
    published_at: str | None = None
    validators: dict[str, str] = field(default_factory=dict)


def _mb(value: int | None) -> str:
    return "?" if value is None else f"{value / 1_000_000:.1f}"


# --- direct HTTPS ----------------------------------------------------------------


def _pick_extension(path_hint: str | None, content_type: str) -> str | None:
    if path_hint in MEDIA_EXTENSIONS:
        return path_hint
    return CONTENT_TYPE_EXTENSIONS.get(content_type)


def _validate_hop(url: str) -> tuple[str, str]:
    """Scheme/userinfo/port checks for every hop; returns ``(host, url)``."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UnsafeUrlError("a redirect left https:// — refusing to follow it")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("a redirect carried credentials — refusing to follow it")
    if parts.port not in (None, 443):
        raise UnsafeUrlError("a redirect moved off port 443 — refusing to follow it")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("a redirect had no host — refusing to follow it")
    return host, urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def _pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    netloc = f"[{address}]" if ":" in address else address
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def download_direct(
    source: UrlSource,
    dest_dir: Path,
    *,
    max_bytes: int,
    report: ProgressFn,
    deadline_s: float = DOWNLOAD_DEADLINE_S,
) -> Downloaded:
    """Fetch one public HTTPS media file under the destination gate and caps."""
    import httpx

    secrets = list(source.secrets)
    url = source.request_url
    redirects = 0
    started = time.monotonic()
    headers = {"User-Agent": f"talkthrough-mcp/{__version__}", "Accept": "*/*"}
    timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=30.0, pool=30.0)
    client_kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "timeout": timeout,
        "headers": headers,
    }
    if _TRANSPORT is not None:  # tests inject an httpx.MockTransport here
        client_kwargs["transport"] = _TRANSPORT
    try:
        with httpx.Client(**client_kwargs) as client:
            while True:
                host, url = _validate_hop(url)
                secrets.append(url)
                report("resolving destination", 0.03)
                address = resolve_public_host(host)[0]
                pinned = _pinned_url(url, address)
                with client.stream(
                    "GET",
                    pinned,
                    headers={"Host": host},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("the server redirected without a Location header")
                        redirects += 1
                        if redirects > MAX_REDIRECTS:
                            raise UnsafeUrlError(
                                f"more than {MAX_REDIRECTS} redirects — refusing to follow"
                            )
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise DownloadError(
                            f"the server answered HTTP {response.status_code} for "
                            f"{source.safe_label()}"
                        )
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                    )
                    extension = _pick_extension(source.path_extension, content_type)
                    if extension is None:
                        raise UnsupportedUrlError(
                            f"the response is not supported media (content-type "
                            f"{content_type or 'unknown'!r}; the URL path has no media "
                            "extension) — pass a direct link to an mp4/mov/webm/mkv/m4a/mp3/"
                            "wav/ogg/flac file, or a YouTube video URL"
                        )
                    length_raw = response.headers.get("content-length")
                    length: int | None = None
                    if length_raw and length_raw.isdigit():
                        length = int(length_raw)
                        if length > max_bytes:
                            raise DownloadError(
                                f"the file is {length} bytes, above the {max_bytes} byte cap "
                                "(TALKTHROUGH_MAX_DOWNLOAD_BYTES)"
                            )
                    check_free_disk(
                        dest_dir,
                        (length * 2) if length is not None else MIN_FREE_BYTES_UNKNOWN_SIZE,
                        what="the download",
                    )
                    part = dest_dir / f"download{extension}.part"
                    final = dest_dir / f"download{extension}"
                    written = 0
                    next_disk_check = DISK_CHECK_EVERY_BYTES
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            for chunk in response.iter_bytes(CHUNK_BYTES):
                                written += len(chunk)
                                if written > max_bytes:
                                    raise DownloadError(
                                        f"download exceeded the {max_bytes} byte cap "
                                        "(TALKTHROUGH_MAX_DOWNLOAD_BYTES) — aborted"
                                    )
                                if time.monotonic() - started > deadline_s:
                                    raise DownloadError(
                                        f"download exceeded {deadline_s:.0f}s — aborted"
                                    )
                                handle.write(chunk)
                                if written >= next_disk_check:
                                    next_disk_check += DISK_CHECK_EVERY_BYTES
                                    check_free_disk(
                                        dest_dir, max(1, (length or written) - written),
                                        what="the download",
                                    )
                                fraction = (written / length) if length else 0.0
                                report(
                                    f"downloading source: {_mb(written)}/{_mb(length)} MB",
                                    0.05 + 0.08 * min(1.0, fraction),
                                )
                    except BaseException:
                        part.unlink(missing_ok=True)
                        raise
                    if length is not None and written != length:
                        part.unlink(missing_ok=True)
                        raise DownloadError(
                            f"truncated download: {written} of {length} bytes — retry"
                        )
                    os.replace(part, final)
                    validators = {
                        key: response.headers[key]
                        for key in ("etag", "last-modified")
                        if key in response.headers
                    }
                    return Downloaded(
                        path=final,
                        extension=extension,
                        downloaded_bytes=written,
                        downloader=f"httpx {httpx.__version__}",
                        validators=validators,
                    )
    except (DownloadError, UnsafeUrlError, UnsupportedUrlError, ValidationError):
        raise
    except httpx.TimeoutException as exc:
        raise DownloadError(
            f"network timeout while downloading {source.safe_label()}: "
            f"{_bounded_reason(str(exc), *secrets)}"
        ) from exc
    except httpx.HTTPError as exc:
        reason = _bounded_reason(str(exc), *secrets)
        hint = (
            " — on a TLS-inspecting corporate network set SSL_CERT_FILE"
            if "certificate" in reason.lower() or "ssl" in reason.lower()
            else ""
        )
        raise DownloadError(
            f"network error while downloading {source.safe_label()}: {reason}{hint}"
        ) from exc
    except OSError as exc:
        raise DownloadError(
            f"could not write the download: {_bounded_reason(str(exc), *secrets)}"
        ) from exc


# --- YouTube via yt-dlp -----------------------------------------------------------


def yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _deno_path() -> str | None:
    """The PyPI-distributed Deno binary, when the [url] extra installed it."""
    try:
        import deno
    except ImportError:
        return None
    try:
        found = deno.find_deno_bin()
    except Exception:  # pragma: no cover - depends on the wheel layout
        return None
    return str(found) if found else None


class _RedactingLogger:
    """yt-dlp logger that keeps signed URLs out of the server log."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = secrets

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            return
        logger.debug("yt-dlp: %s", redact(message, *self._secrets))

    def info(self, message: str) -> None:
        logger.debug("yt-dlp: %s", redact(message, *self._secrets))

    def warning(self, message: str) -> None:
        logger.info("yt-dlp warning: %s", redact(message, *self._secrets))

    def error(self, message: str) -> None:
        logger.warning("yt-dlp error: %s", redact(message, *self._secrets))


def youtube_options(
    dest_dir: Path,
    *,
    video_id: str,
    max_bytes: int,
    progress_hook: Callable[[dict[str, Any]], None],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    """The allowlisted YoutubeDL option set — nothing else is ever passed."""
    from . import jobs

    options: dict[str, Any] = {
        "noplaylist": True,
        "playlist_items": "1",
        "max_downloads": 1,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _RedactingLogger(secrets),
        "color": {"stdout": "no_color", "stderr": "no_color"},
        "format": YOUTUBE_FORMAT,
        "outtmpl": {"default": str(dest_dir / f"youtube-{video_id}.%(ext)s")},
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": max_bytes,
        "socket_timeout": CONNECT_TIMEOUT_S,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "continuedl": False,
        "overwrites": True,
        "progress_hooks": [progress_hook],
        "ffmpeg_location": str(Path(ffmpeg_path()).parent),
        "cookiesfrombrowser": None,
        "cookiefile": None,
        "remote_components": [],
        "extractor_args": {},
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
        "writedescription": False,
        "writecomments": False,
        "getcomments": False,
        "cachedir": str(jobs.talkthrough_home() / "cache" / "yt-dlp"),
        "ignoreerrors": False,
    }
    deno = _deno_path()
    if deno is not None:
        options["js_runtimes"] = {"deno": {"path": deno}}
    return options


def _disable_yt_dlp_plugins() -> None:
    """Never load third-party yt-dlp plugins from the user's environment."""
    try:
        from yt_dlp import globals as yt_globals

        yt_globals.plugin_dirs.value = []
    except Exception as exc:  # pragma: no cover - depends on yt-dlp internals
        logger.info("could not disable yt-dlp plugin directories: %s", exc)


def _published_at(info: dict[str, Any]) -> str | None:
    for key in ("release_timestamp", "timestamp"):
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat(timespec="seconds")
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return None


def _youtube_restriction(message: str) -> str | None:
    """Map a yt-dlp failure to one bounded, actionable category."""
    lowered = message.lower()
    if "age" in lowered and ("confirm" in lowered or "restricted" in lowered):
        return "the video is age-restricted — sign-in based access is not supported"
    if "private video" in lowered or "sign in" in lowered or "login" in lowered:
        return (
            "the video is private, members-only or requires sign-in — cookies and logins "
            "are not supported"
        )
    if "live" in lowered and ("begin" in lowered or "stream" in lowered or "live event" in lowered):
        return "the video is a live stream — only completed recordings are supported"
    if "drm" in lowered:
        return "the video is DRM-protected — not supported"
    if "in your country" in lowered or "geo" in lowered or "region" in lowered:
        return "the video is not available in this region — no bypass is attempted"
    if "unavailable" in lowered or "removed" in lowered or "does not exist" in lowered:
        return "the video is unavailable or was removed"
    if "premium" in lowered or "members" in lowered:
        return "the video is members-only or premium — not supported"
    return None


def preflight_info(info: dict[str, Any], *, max_seconds: int, max_bytes: int) -> int | None:
    """Reject playlists, live streams, restricted videos and cap breaches.

    Returns the estimated download size when the provider reports one.
    """
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise UnsupportedUrlError(
            "the URL resolved to a playlist or a multi-video page — pass one video URL"
        )
    live_status = str(info.get("live_status") or "")
    if info.get("is_live") or live_status in {"is_live", "is_upcoming", "post_live"}:
        raise UnsupportedUrlError(
            "the video is a live stream or has not finished processing yet — only completed "
            "recordings are supported; retry after the recording is available"
        )
    availability = str(info.get("availability") or "")
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        raise UnsupportedUrlError(
            f"the video is {availability.replace('_', ' ')} — cookies, logins and "
            "memberships are not supported"
        )
    age_limit = info.get("age_limit")
    if isinstance(age_limit, int) and not isinstance(age_limit, bool) and age_limit >= 18:
        raise UnsupportedUrlError(
            "the video is age-restricted — sign-in based access is not supported"
        )
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        raise UnsupportedUrlError(
            "the provider reports no duration for this video — only completed recordings "
            "with a known length are supported"
        )
    if duration > max_seconds:
        raise ValidationError(
            f"duration {duration:.0f}s exceeds the {max_seconds}s cap "
            "(override with TALKTHROUGH_MAX_SECONDS)"
        )
    estimate = 0
    formats = info.get("requested_formats") or [info]
    for entry in formats:
        if not isinstance(entry, dict):
            continue
        size = entry.get("filesize") or entry.get("filesize_approx")
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            estimate += int(size)
    if estimate > max_bytes:
        raise DownloadError(
            f"the provider estimates {estimate} bytes, above the {max_bytes} byte cap "
            "(TALKTHROUGH_MAX_DOWNLOAD_BYTES)"
        )
    return estimate or None


def download_youtube(
    source: UrlSource,
    dest_dir: Path,
    *,
    max_bytes: int,
    max_seconds: int,
    report: ProgressFn,
) -> Downloaded:
    """Preflight, then download one public YouTube video via yt-dlp."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise UrlExtraMissingError(_MISSING_EXTRA) from exc
    assert source.canonical_url is not None and source.provider_id is not None
    secrets = source.secrets
    _disable_yt_dlp_plugins()
    cap_hit: list[str] = []
    started = time.monotonic()

    def progress_hook(status: dict[str, Any]) -> None:
        downloaded = status.get("downloaded_bytes")
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if isinstance(downloaded, (int, float)) and downloaded > max_bytes:
            cap_hit.append("bytes")
            raise DownloadError(
                f"download exceeded the {max_bytes} byte cap (TALKTHROUGH_MAX_DOWNLOAD_BYTES) "
                "— aborted"
            )
        if time.monotonic() - started > DOWNLOAD_DEADLINE_S:
            cap_hit.append("time")
            raise DownloadError(f"download exceeded {DOWNLOAD_DEADLINE_S:.0f}s — aborted")
        if status.get("status") == "downloading" and isinstance(downloaded, (int, float)):
            total_int = int(total) if isinstance(total, (int, float)) else None
            fraction = (downloaded / total_int) if total_int else 0.0
            report(
                f"downloading source: {_mb(int(downloaded))}/{_mb(total_int)} MB",
                0.05 + 0.08 * min(1.0, fraction),
            )

    options = youtube_options(
        dest_dir,
        video_id=source.provider_id,
        max_bytes=max_bytes,
        progress_hook=progress_hook,
        secrets=secrets,
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            report("checking provider metadata and caps", 0.04)
            info = ydl.extract_info(source.canonical_url, download=False)
            if not isinstance(info, dict):
                raise DownloadError("the provider returned no metadata for this video")
            estimate = preflight_info(info, max_seconds=max_seconds, max_bytes=max_bytes)
            check_free_disk(
                dest_dir,
                (estimate * 2) if estimate else MIN_FREE_BYTES_UNKNOWN_SIZE,
                what="the download",
            )
            report("downloading source", 0.05)
            ydl.process_ie_result(info, download=True)
            produced = _produced_file(info, dest_dir, source.provider_id)
    except (DownloadError, UnsafeUrlError, UnsupportedUrlError, ValidationError):
        raise
    except yt_dlp.utils.YoutubeDLError as exc:
        if cap_hit:
            raise DownloadError(
                f"download exceeded the {max_bytes} byte cap (TALKTHROUGH_MAX_DOWNLOAD_BYTES) "
                "— aborted"
                if cap_hit[0] == "bytes"
                else f"download exceeded {DOWNLOAD_DEADLINE_S:.0f}s — aborted"
            ) from exc
        message = _bounded_reason(str(exc), *secrets)
        restriction = _youtube_restriction(message)
        if restriction is not None:
            raise UnsupportedUrlError(f"{source.safe_label()}: {restriction}") from exc
        raise DownloadError(
            f"the provider extraction failed for {source.safe_label()}: {message}"
        ) from exc
    except Exception as exc:  # anything else from the downloader stack
        if cap_hit:
            raise DownloadError(
                f"download exceeded the {max_bytes} byte cap (TALKTHROUGH_MAX_DOWNLOAD_BYTES) "
                "— aborted"
            ) from exc
        raise DownloadError(
            f"unexpected downloader failure for {source.safe_label()}: "
            f"{type(exc).__name__}: {_bounded_reason(str(exc), *secrets)}"
        ) from exc
    size = produced.stat().st_size
    if size > max_bytes:
        produced.unlink(missing_ok=True)
        raise DownloadError(
            f"the downloaded file is {size} bytes, above the {max_bytes} byte cap "
            "(TALKTHROUGH_MAX_DOWNLOAD_BYTES)"
        )
    return Downloaded(
        path=produced,
        extension=produced.suffix.lower(),
        downloaded_bytes=size,
        downloader=f"yt-dlp {yt_dlp.version.__version__}",
        title=info.get("title") if isinstance(info.get("title"), str) else None,
        published_at=_published_at(info),
    )


def _produced_file(info: dict[str, Any], dest_dir: Path, video_id: str) -> Path:
    """The merged output yt-dlp wrote, found without trusting remote names."""
    for entry in info.get("requested_downloads") or []:
        if isinstance(entry, dict):
            candidate = entry.get("filepath")
            if isinstance(candidate, str) and Path(candidate).is_file():
                path = Path(candidate)
                if path.parent == dest_dir and path.suffix.lower() in MEDIA_EXTENSIONS:
                    return path
    candidates = [
        path
        for path in dest_dir.glob(f"youtube-{video_id}.*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    ]
    if len(candidates) == 1:
        return candidates[0]
    for path in dest_dir.iterdir():
        if path.is_file() and path.suffix == ".part":
            path.unlink(missing_ok=True)
    raise DownloadError(
        "the provider download produced no supported media file (mp4/webm/mkv) — the "
        "format may need a JavaScript runtime; see docs/TROUBLESHOOTING.md"
    )


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
