"""URL ingestion: turn one public media URL into a managed local source.

This is the server's only runtime network boundary (0.4.0). Everything here
happens BEFORE the deterministic pipeline: classify the URL, prove the
destination is public, download once into a private staging directory under
hard caps, verify the bytes with ffprobe, install the file inside the job it
hashes to, and remember the URL → job mapping without ever storing the raw
URL. After that the local pipeline and every retrieval tool run exactly as
they do for a file the user downloaded by hand.

Rules that are contracts, not preferences:

- ``process_media`` stays local-only; this module never widens it.
- Raw URLs, query strings, signed tokens and userinfo never reach a manifest,
  the URL index, a log line, a progress message or an error. Every outbound
  string passes through :func:`redact`.
- Job identity stays content-addressed (SHA-256 of the media bytes), so the
  same video reached through two URLs converges on one job, and a local
  file processed earlier is reused instead of rebuilt.
- Provider publication time is provider metadata, never a recording start:
  it is stored under ``media.origin.published_at`` and never becomes
  ``wall_clock``. Download time is not a wall-clock rung either.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, parse_qs, urlsplit, urlunsplit

from . import jobs
from .errors import ToolFailureError, ValidationError
from .manifest import MediaOrigin, atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024**3  # 2 GiB
MAX_REDIRECTS = 5
SOURCE_DIR_NAME = "source"
DOWNLOADS_DIR_NAME = "downloads"
URLS_DIR_NAME = "urls"
TITLE_MAX_CHARS = 200

KIND_YOUTUBE = "youtube"
KIND_DIRECT = "direct_url"

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_SINGLE_VIDEO_PATHS = ("shorts", "live", "embed", "v")
_YOUTUBE_REJECTED_PATHS = {
    "playlist": "a playlist URL",
    "channel": "a channel URL",
    "c": "a channel URL",
    "user": "a channel URL",
    "results": "a search URL",
    "feed": "a feed URL",
    "hashtag": "a hashtag URL",
}
# Extensions the local pipeline accepts; a direct URL must end up as one of
# these after ffprobe agrees the bytes are media.
VIDEO_EXTENSIONS = frozenset({".mov", ".mp4", ".webm", ".mkv"})
AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav", ".ogg", ".flac"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}

_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ftp|rtmp|rtmps|rtsp|wss?)://[^\s'\"<>]+")
_REDACTED = "<url>"


# --- errors ---------------------------------------------------------------------


class UnsupportedUrlError(ValidationError):
    """The URL shape is not one this release supports (or is not a URL)."""


class UnsafeUrlError(ValidationError):
    """The URL or its resolution would reach a destination we refuse to touch."""


class UrlExtraMissingError(ValidationError):
    """A provider adapter needs the optional ``[url]`` extra."""


class DownloadError(ToolFailureError):
    """Network, provider or cap failure while fetching a source."""


# --- redaction --------------------------------------------------------------------


def redact(text: str, *secrets: str) -> str:
    """Strip every URL-shaped token and every known secret from ``text``.

    Provider libraries put signed media URLs into their exception messages;
    the raw input carries query tokens. Neither may leave this module.
    """
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, _REDACTED)
    return _URL_PATTERN.sub(_REDACTED, result)


def _bounded_reason(text: str, *secrets: str, limit: int = 240) -> str:
    first_line = redact(text, *secrets).strip().splitlines()[:1]
    reason = first_line[0] if first_line else "no detail"
    return reason if len(reason) <= limit else reason[: limit - 1] + "…"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- classification ---------------------------------------------------------------


@dataclass(frozen=True)
class UrlSource:
    """One classified input URL. ``request_url`` lives in memory only."""

    kind: str
    provider: str
    mapping_key: str
    url_sha256: str
    request_url: str
    provider_id: str | None = None
    host: str | None = None
    canonical_url: str | None = None
    path_extension: str | None = None

    @property
    def secrets(self) -> tuple[str, ...]:
        """Strings that must never appear in any outbound text."""
        parts = urlsplit(self.request_url)
        return tuple(value for value in (self.request_url, parts.query) if value)

    def safe_label(self) -> str:
        if self.kind == KIND_YOUTUBE:
            return f"YouTube video {self.provider_id}"
        return f"https://{self.host}/… (direct media URL)"


def classify_url(raw: str) -> UrlSource:
    """Classify one input URL or refuse it with a bounded, secret-free reason."""
    text = raw.strip()
    if not text:
        raise UnsupportedUrlError(
            "url is empty — pass one public https:// media URL or one YouTube video URL"
        )
    if any(character.isspace() or ord(character) < 32 for character in text):
        raise UnsupportedUrlError("url contains whitespace or control characters")
    if text.startswith(("/", "~", ".")) or re.match(r"^[A-Za-z]:\\", text):
        raise UnsupportedUrlError(
            "that is a local path — local files are handled by process_media(path=...), "
            "not process_url"
        )
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 — validates the port syntax eagerly
    except ValueError as exc:
        raise UnsupportedUrlError(f"malformed URL ({exc})") from exc
    scheme = parts.scheme.lower()
    if scheme == "file":
        raise UnsupportedUrlError(
            "file:// URLs are not accepted — local files are handled by process_media(path=...)"
        )
    if scheme not in {"https", "http"}:
        raise UnsupportedUrlError(
            f"unsupported URL scheme {scheme or '(none)'!r} — only https:// direct media URLs "
            "and YouTube video URLs are supported"
        )
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError(
            "credentials inside the URL (user:pass@host) are not accepted; no cookies, "
            "logins or custom headers are sent"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsupportedUrlError("url has no host")
    if host in _YOUTUBE_HOSTS:
        return _classify_youtube(text, parts, host)
    if scheme != "https":
        raise UnsupportedUrlError(
            "only https:// direct media URLs are supported (plain http:// is refused)"
        )
    return _classify_direct(text, parts, host)


def _youtube_video_id(parts: SplitResult, host: str) -> str:
    segments = [segment for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query, keep_blank_values=False)
    if host in {"youtu.be", "www.youtu.be"}:
        if not segments:
            raise UnsupportedUrlError("youtu.be link without a video id")
        return segments[0]
    if not segments or segments[0] == "watch":
        values = query.get("v") or []
        if not values:
            if query.get("list"):
                raise UnsupportedUrlError(
                    "a playlist URL without a single video id is not supported — pass "
                    "one video URL (watch?v=…, youtu.be/…, shorts/…)"
                )
            raise UnsupportedUrlError("YouTube URL without a video id (expected watch?v=…)")
        return values[0]
    head = segments[0]
    if head.startswith("@"):
        raise UnsupportedUrlError(
            "a channel URL is not supported — pass one video URL, not a channel, "
            "playlist or search"
        )
    if head in _YOUTUBE_REJECTED_PATHS:
        raise UnsupportedUrlError(
            f"{_YOUTUBE_REJECTED_PATHS[head]} is not supported — pass one video URL, "
            "not a channel, playlist or search"
        )
    if head in _YOUTUBE_SINGLE_VIDEO_PATHS and len(segments) >= 2:
        return segments[1]
    raise UnsupportedUrlError(
        "unrecognized YouTube URL shape — supported: watch?v=ID, youtu.be/ID, shorts/ID, "
        "live/ID, embed/ID"
    )


def _classify_youtube(text: str, parts: SplitResult, host: str) -> UrlSource:
    video_id = _youtube_video_id(parts, host)
    if not _YOUTUBE_ID.match(video_id):
        raise UnsupportedUrlError("YouTube video id has an unexpected shape")
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return UrlSource(
        kind=KIND_YOUTUBE,
        provider="youtube",
        mapping_key=f"youtube:{video_id}",
        url_sha256=_sha256_text(text),
        request_url=canonical,
        provider_id=video_id,
        host="www.youtube.com",
        canonical_url=canonical,
    )


def _classify_direct(text: str, parts: SplitResult, host: str) -> UrlSource:
    if parts.port not in (None, 443):
        raise UnsafeUrlError(
            f"port {parts.port} is not accepted — direct media URLs must use https on port 443"
        )
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_address(literal):
        raise UnsafeUrlError(
            "the URL points at a private, loopback, link-local or reserved address — "
            "only public hosts are downloaded"
        )
    netloc = parts.netloc  # no userinfo (checked by the caller), port already validated
    request_url = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    suffix = Path(parts.path).suffix.lower()
    return UrlSource(
        kind=KIND_DIRECT,
        provider=host,
        mapping_key=f"direct:{_sha256_text(text)}",
        url_sha256=_sha256_text(text),
        request_url=request_url,
        host=host,
        path_extension=suffix if suffix in MEDIA_EXTENSIONS else None,
    )


# --- network destination gate (SSRF) ----------------------------------------------


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address | str) -> bool:
    """True only for globally routable unicast addresses.

    Private, loopback, link-local (cloud metadata lives there), multicast,
    reserved, unspecified, shared-address-space (100.64/10) and IPv4-mapped
    IPv6 forms of any of those are all refused.
    """
    ip = ipaddress.ip_address(address) if isinstance(address, str) else address
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        ip = ip.sixtofour
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def resolve_public_host(host: str) -> list[str]:
    """Resolve ``host`` and require EVERY returned address to be public.

    A name that resolves to one public and one private address is refused
    outright — a resolver may hand a connection either one.
    """
    bare = host.strip("[]")
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_address(literal):
            raise UnsafeUrlError(
                "the URL points at a private, loopback, link-local or reserved address — "
                "only public hosts are downloaded"
            )
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(bare, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DownloadError(
            f"could not resolve host {bare!r} ({exc.strerror or exc}) — check the URL and "
            "the network"
        ) from exc
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        candidate = str(sockaddr[0])
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise DownloadError(f"host {bare!r} resolved to no addresses")
    for candidate in addresses:
        if not is_public_address(candidate):
            raise UnsafeUrlError(
                f"host {bare!r} resolves to a non-public address — refusing to download "
                "from private, loopback, link-local or reserved networks"
            )
    return addresses


# --- caps -------------------------------------------------------------------------


def max_download_bytes() -> int:
    raw = os.environ.get("TALKTHROUGH_MAX_DOWNLOAD_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_DOWNLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring non-integer TALKTHROUGH_MAX_DOWNLOAD_BYTES=%r", raw)
        return DEFAULT_MAX_DOWNLOAD_BYTES
    if value <= 0:
        logger.warning("ignoring non-positive TALKTHROUGH_MAX_DOWNLOAD_BYTES=%r", raw)
        return DEFAULT_MAX_DOWNLOAD_BYTES
    return value


def check_free_disk(directory: Path, needed_bytes: int, *, what: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(directory)).free
    if free < needed_bytes:
        raise ValidationError(
            f"free disk {free} bytes is below the {needed_bytes} bytes needed for {what} — "
            "free up space and retry"
        )


def bounded_title(title: object) -> str | None:
    """A display-only title: control characters removed, length capped."""
    if not isinstance(title, str):
        return None
    cleaned = "".join(
        character
        for character in unicodedata.normalize("NFC", title)
        if unicodedata.category(character)[0] != "C"
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= TITLE_MAX_CHARS else cleaned[: TITLE_MAX_CHARS - 1] + "…"


# --- URL index ------------------------------------------------------------------


@dataclass(frozen=True)
class UrlMapping:
    job_id: str
    provider: str
    created_at: str
    provider_id: str | None = None
    validators: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "provider": self.provider,
            "created_at": self.created_at,
        }
        if self.provider_id is not None:
            payload["provider_id"] = self.provider_id
        if self.validators:
            payload["validators"] = dict(self.validators)
        return payload

    @staticmethod
    def from_dict(payload: object) -> UrlMapping | None:
        if not isinstance(payload, dict):
            return None
        job_id = payload.get("job_id")
        provider = payload.get("provider")
        created_at = payload.get("created_at")
        if not (isinstance(job_id, str) and isinstance(provider, str)):
            return None
        provider_id = payload.get("provider_id")
        validators_raw = payload.get("validators")
        validators = (
            {str(k): str(v) for k, v in validators_raw.items()}
            if isinstance(validators_raw, dict)
            else {}
        )
        return UrlMapping(
            job_id=job_id,
            provider=provider,
            created_at=str(created_at) if created_at is not None else "",
            provider_id=str(provider_id) if isinstance(provider_id, str) else None,
            validators=validators,
        )


def urls_root() -> Path:
    return jobs.talkthrough_home() / URLS_DIR_NAME


def downloads_root() -> Path:
    return jobs.talkthrough_home() / DOWNLOADS_DIR_NAME


def mapping_path(mapping_key: str) -> Path:
    """The index file for one key — a hash, so no raw URL lands in a filename."""
    return urls_root() / f"{_sha256_text(mapping_key)[:32]}.json"


def load_mapping(mapping_key: str) -> UrlMapping | None:
    """The live mapping for ``mapping_key``; a mapping to a deleted job is dropped."""
    path = mapping_path(mapping_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("dropping unreadable URL mapping %s: %s", path.name, exc)
        path.unlink(missing_ok=True)
        return None
    mapping = UrlMapping.from_dict(payload)
    if mapping is None or not jobs.job_exists(mapping.job_id):
        path.unlink(missing_ok=True)
        return None
    return mapping


def save_mapping(mapping_key: str, mapping: UrlMapping) -> Path:
    path = mapping_path(mapping_key)
    atomic_write_text(path, json.dumps(mapping.to_dict(), ensure_ascii=False, indent=2))
    return path


def sweep_stale_mappings() -> list[str]:
    """Drop index entries whose job no longer exists (gc)."""
    root = urls_root()
    if not root.is_dir():
        return []
    removed: list[str] = []
    for path in sorted(root.glob("*.json")):
        try:
            mapping = UrlMapping.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            mapping = None
        if mapping is None or not jobs.job_exists(mapping.job_id):
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def sweep_stale_downloads(min_age_s: float = jobs.PARTIAL_SWEEP_MIN_AGE_S) -> list[str]:
    """Remove download staging directories a dead process left behind (gc)."""
    root = downloads_root()
    if not root.is_dir():
        return []
    import time

    now = time.time()
    removed: list[str] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        try:
            if now - directory.stat().st_mtime < min_age_s:
                continue
        except OSError:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(directory.name)
    return removed


_URL_LOCKS_GUARD = threading.Lock()
_URL_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def url_lock(mapping_key: str, *, wait_seconds: int = 3600) -> Iterator[None]:
    """Serialize concurrent ingestion of the same URL (threads and processes).

    The second caller waits for the first and then finds the mapping instead
    of downloading the same source twice. Cross-process: POSIX flock on an
    index-side lock file (no-op on platforms without fcntl).
    """
    with _URL_LOCKS_GUARD:
        process_lock = _URL_LOCKS.setdefault(mapping_key, threading.Lock())
    if not process_lock.acquire(timeout=max(0, wait_seconds)):
        raise ToolFailureError(
            "another thread has been ingesting this URL for over "
            f"{wait_seconds}s — retry later"
        )
    try:
        lock_path = mapping_path(mapping_key).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows best-effort
            # No flock semantics; keep the on-disk layout identical and rely
            # on the process-level lock (the job lock degrades the same way).
            lock_path.touch(exist_ok=True)
            yield
            return
        import time

        with lock_path.open("w") as handle:
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ToolFailureError(
                            "another process has been ingesting this URL for over "
                            f"{wait_seconds}s — retry later"
                        ) from None
                    time.sleep(1)
            yield
    finally:
        process_lock.release()


# --- managed source -----------------------------------------------------------


@contextmanager
def download_workspace() -> Iterator[Path]:
    """A private staging directory on the job store's filesystem."""
    root = downloads_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dl-", dir=root))
    try:
        yield staging
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def managed_source_name(source: UrlSource, extension: str) -> str:
    """Talkthrough names the file — never the remote title."""
    if source.kind == KIND_YOUTUBE:
        return f"youtube-{source.provider_id}{extension}"
    return f"direct-{source.url_sha256[:12]}{extension}"


def managed_source_relative(name: str) -> str:
    return f"{SOURCE_DIR_NAME}/{name}"


def install_managed_source(job_id: str, downloaded: Path, name: str) -> Path:
    """Move a verified download into ``jobs/<job_id>/source/<name>``.

    Same filesystem, atomic replace. Re-installing identical bytes is
    harmless (content-addressed job), so two URLs that converge on one job
    may both install without coordination.
    """
    target_dir = jobs.job_dir(job_id) / SOURCE_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    try:
        os.replace(downloaded, target)
    except OSError:
        shutil.move(str(downloaded), str(target))
    return target


def source_path(manifest_media_path: str, job_id: str, managed_source: str | None) -> Path:
    """The file ``extract_frame`` should decode: managed source first."""
    if managed_source:
        candidate = jobs.job_dir(job_id) / managed_source
        if candidate.is_file():
            return candidate
    return Path(manifest_media_path)


def build_origin(
    source: UrlSource,
    *,
    downloader: str,
    downloaded_bytes: int,
    title: str | None,
    published_at: str | None,
) -> MediaOrigin:
    return MediaOrigin(
        kind=source.kind,
        provider=source.provider,
        url_sha256=source.url_sha256,
        provider_id=source.provider_id,
        host=source.host,
        title=bounded_title(title),
        published_at=published_at,
        downloader=downloader,
        downloaded_bytes=downloaded_bytes,
        downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def attach_managed_source(job_id: str, managed_source: str, origin: MediaOrigin) -> None:
    """Record a managed source on a job that already existed (same bytes).

    A local file processed earlier and a URL that downloads the same bytes
    converge on one job: the manifest gains the managed path and, if it had
    none, the origin — additively, under the job lock.
    """
    from dataclasses import replace

    from .manifest import save_manifest

    with jobs.job_lock(job_id), jobs.partial_job_cleanup(job_id):
        jobs.recover_interrupted_reprocess(job_id)
        manifest = jobs.load_job(job_id)
        manifest.media = replace(
            manifest.media,
            origin=manifest.media.origin or origin,
            managed_source=managed_source,
        )
        save_manifest(manifest, jobs.job_dir(job_id))


# --- orchestration ----------------------------------------------------------------


@dataclass(frozen=True)
class UrlProcessResult:
    """A processed URL: the pipeline result plus what the network step did."""

    result: Any  # pipeline.ProcessResult (kept untyped to avoid an import cycle)
    source: UrlSource
    origin: MediaOrigin | None
    reused_url_mapping: bool
    refreshed: bool
    downloaded_bytes: int | None


def process_url(
    url: str,
    *,
    refresh: bool = False,
    force: bool = False,
    recorded_at: str | None = None,
    vocabulary: str | None = None,
    language: str | None = None,
    model: str | None = None,
    diarize_speakers: bool | None = None,
    num_speakers: int | None = None,
    progress: Any = None,
) -> UrlProcessResult:
    """Download one public URL once, then run the local pipeline on the file.

    The download owns the first 15% of the progress range; the pipeline's own
    stages are renormalized onto 15-100%. ``refresh=False`` serves a stored
    job for a known URL without any network; ``refresh=True`` downloads
    again and may land on a different job when the bytes changed.
    """
    from . import pipeline, url_download
    from .probe import probe_media

    def report(stage: str, fraction: float) -> None:
        if progress is not None:
            progress(stage, max(0.0, min(1.0, fraction)))

    def pipeline_report(stage: str, fraction: float) -> None:
        report(stage, 0.15 + 0.85 * fraction)

    report("validating URL", 0.01)
    source = classify_url(url)
    analysis: dict[str, Any] = {
        "recorded_at": recorded_at,
        "vocabulary": vocabulary,
        "language": language,
        "model": model,
        "diarize_speakers": diarize_speakers,
        "num_speakers": num_speakers,
        # ``force`` rebuilds the stored job from the kept source (re-anchor
        # recorded_at, change the model); ``refresh`` re-downloads instead.
        "force": force,
    }
    with url_lock(source.mapping_key):
        if not refresh:
            mapping = load_mapping(source.mapping_key)
            if mapping is not None:
                stored, _unreadable = jobs.load_previous_job(mapping.job_id)
                if stored is not None:
                    path = source_path(
                        stored.media.path, mapping.job_id, stored.media.managed_source
                    )
                    if path.is_file():
                        logger.info(
                            "%s already ingested as job %s — no network",
                            source.safe_label(),
                            mapping.job_id,
                        )
                        result = pipeline.process_media(
                            str(path), **analysis, progress=pipeline_report
                        )
                        return UrlProcessResult(
                            result=result,
                            source=source,
                            origin=result.manifest.media.origin,
                            reused_url_mapping=True,
                            refreshed=False,
                            downloaded_bytes=None,
                        )
                    logger.warning(
                        "managed source of job %s is gone — downloading %s again",
                        mapping.job_id,
                        source.safe_label(),
                    )
        report("resolving provider", 0.02)
        max_bytes = max_download_bytes()
        max_seconds = pipeline.max_seconds_cap()
        with download_workspace() as staging:
            if source.kind == KIND_YOUTUBE:
                downloaded = url_download.download_youtube(
                    source, staging, max_bytes=max_bytes, max_seconds=max_seconds, report=report
                )
            else:
                downloaded = url_download.download_direct(
                    source, staging, max_bytes=max_bytes, report=report
                )
            report("verifying media", 0.14)
            info = probe_media(downloaded.path)
            if info.duration_s <= 0:
                raise UnsupportedUrlError(
                    "the downloaded file is not playable media (no duration) — pass a direct "
                    "link to a media file or a YouTube video URL"
                )
            if info.duration_s > max_seconds:
                raise ValidationError(
                    f"duration {info.duration_s:.0f}s exceeds the {max_seconds}s cap "
                    "(override with TALKTHROUGH_MAX_SECONDS)"
                )
            job_id = jobs.compute_job_id(downloaded.path)
            name = managed_source_name(source, downloaded.extension)
            relative = managed_source_relative(name)
            origin = build_origin(
                source,
                downloader=downloaded.downloader,
                downloaded_bytes=downloaded.downloaded_bytes,
                title=downloaded.title,
                published_at=downloaded.published_at,
            )
            mapping = UrlMapping(
                job_id=job_id,
                provider=source.provider,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                provider_id=source.provider_id,
                validators=downloaded.validators,
            )
            # Install and process under ONE job lock (re-entrant for the
            # pipeline's own acquisition): another URL with the same bytes
            # waits here instead of installing into a directory that this
            # call's failure cleanup could remove from under it. The mapping
            # is written as soon as the file is in place, so a refusal or a
            # failure after this point never costs a second download; a
            # mapping to a job that never got its manifest is dropped lazily.
            with jobs.job_lock(job_id):
                managed = install_managed_source(job_id, downloaded.path, name)
                save_mapping(source.mapping_key, mapping)
                result = pipeline.process_media(
                    str(managed),
                    **analysis,
                    progress=pipeline_report,
                    origin=origin,
                    managed_source=relative,
                )
                if result.reused and (
                    result.manifest.media.managed_source != relative
                    or result.manifest.media.origin is None
                ):
                    # Same bytes as a job processed earlier (a local file, or
                    # another URL): keep that job, remember the managed copy
                    # and origin.
                    attach_managed_source(job_id, relative, origin)
                    from dataclasses import replace

                    result = replace(result, manifest=jobs.load_job(job_id))
        return UrlProcessResult(
            result=result,
            source=source,
            origin=result.manifest.media.origin or origin,
            reused_url_mapping=False,
            refreshed=refresh,
            downloaded_bytes=downloaded.downloaded_bytes,
        )
