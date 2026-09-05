"""CLI entry point: ``serve`` (default) | ``process <file>`` | ``process-url <url>`` | ``gc``.

``process`` is the debug/batch path: it runs the same pipeline the MCP tool
uses and prints the summary, so long recordings can be pre-processed outside
an agent session and then queried by job_id (the store is content-addressed).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any, NoReturn, TextIO

from .core.errors import TalkthroughError

logger = logging.getLogger(__name__)

# The HTTP client stack logs every request line at INFO with the full URL
# (httpx: ``HTTP Request: GET https://host/path?signed-query "HTTP/1.1 200 OK"``,
# one line per hop, redirect targets included). A signed CDN query must not
# land in stderr, which MCP clients keep as log files.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")
_OWN_LOGGER = "talkthrough_mcp"


class _RedactUrls(logging.Filter):
    """Keep URL-shaped tokens out of the log records other libraries emit.

    The privacy contract says a raw URL — its query and userinfo above all —
    never reaches a log line. Talkthrough's own loggers already pass their text
    through ``url_ingest.redact`` or use a safe label such as ``https://host/…``
    that must survive, so only foreign records are rewritten; a traceback is
    scrubbed whoever logs it (exception messages quote URLs freely).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from .core.url_ingest import redact

        own = record.name == _OWN_LOGGER or record.name.startswith(_OWN_LOGGER + ".")
        if not own:
            message = record.getMessage()
            redacted = redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _privacy_handler(stream: TextIO) -> logging.Handler:
    """The stderr handler: the 0.4.0 line format plus the URL filter."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactUrls())
    return handler


def _configure_logging() -> None:
    """Root INFO to stderr, with the URL privacy contract applied to every record.

    ``basicConfig`` is a no-op when something configured the root logger
    first (an embedding, pytest); the filter is then attached to the handlers
    that exist, and the HTTP client loggers are held at WARNING either way —
    their INFO request lines are noise for a user and a leak for the contract.
    """
    logging.basicConfig(level=logging.INFO, handlers=[_privacy_handler(sys.stderr)])
    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing, _RedactUrls) for existing in handler.filters):
            handler.addFilter(_RedactUrls())
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _extra_status(extra: str, distribution: str, when_missing: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"{extra} extra: {distribution} {version(distribution)}"
    except PackageNotFoundError:
        return f"{extra} extra: not installed{when_missing}"


def version_line() -> str:
    """``talkthrough-mcp <version> (python <x.y.z>; url extra: …; diarization extra: …)``.

    The extras decide what ``process_url`` and ``diarize`` can do, and a
    hand-written ``uvx talkthrough-mcp`` config upgrades the server without
    them — so the line says which ones this environment actually has.
    """
    from . import __version__

    python = ".".join(str(part) for part in sys.version_info[:3])
    url = _extra_status("url", "yt-dlp", " (direct https:// media links only)")
    diarization = _extra_status("diarization", "sherpa-onnx", "")
    return f"talkthrough-mcp {__version__} (python {python}; {url}; {diarization})"


class _VersionAction(argparse.Action):
    """``--version``: the package version plus the state of the optional extras."""

    def __init__(
        self, option_strings: Sequence[str], dest: str, help: str | None = None, **_: Any
    ) -> None:
        super().__init__(
            option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0, help=help
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        print(version_line())
        parser.exit()


class UsageError(Exception):
    """An argparse failure that ``main`` can render in human and JSON forms."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(self, message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="talkthrough-mcp",
        description="Local-first MCP server for narrated screen recordings.",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="print the package version and which optional extras this environment has",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the MCP server on stdio (default)")

    process = sub.add_parser("process", help="run the pipeline on one file and print a summary")
    process.add_argument("file", help="path to a video or audio file")
    process.add_argument("--json", action="store_true", help="print the summary as JSON")
    process.add_argument("--force", action="store_true", help="reprocess even if already done")
    process.add_argument("--recorded-at", default=None, help="ISO 8601 wall-clock override")
    process.add_argument("--language", default=None, help="transcription language (default auto)")
    process.add_argument(
        "--vocabulary", default=None, help="domain terms to bias transcription toward"
    )
    process.add_argument(
        "--model",
        default=None,
        help="whisper model for this run (e.g. large-v3-turbo); default from env/small",
    )
    process.add_argument(
        "--diarize",
        action="store_true",
        help="label who said what (S1/S2/…); needs the [diarization] extra",
    )
    process.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        metavar="N",
        help="exact speaker count when known — the single best quality lever",
    )

    process_url = sub.add_parser(
        "process-url",
        help="download one public video/audio URL once, run the pipeline, print a summary",
    )
    process_url.add_argument("url", help="public https:// media URL or one YouTube video URL")
    process_url.add_argument("--json", action="store_true", help="print the summary as JSON")
    process_url.add_argument(
        "--refresh",
        action="store_true",
        help="download again even if this URL was ingested before",
    )
    process_url.add_argument(
        "--force",
        action="store_true",
        help="rebuild the stored job from the kept source (re-anchor, new model)",
    )
    process_url.add_argument("--recorded-at", default=None, help="ISO 8601 wall-clock override")
    process_url.add_argument(
        "--language", default=None, help="transcription language (default auto)"
    )
    process_url.add_argument(
        "--vocabulary", default=None, help="domain terms to bias transcription toward"
    )
    process_url.add_argument(
        "--model",
        default=None,
        help="whisper model for this run (e.g. large-v3-turbo); default from env/small",
    )
    process_url.add_argument(
        "--diarize",
        action="store_true",
        help="label who said what (S1/S2/…); needs the [diarization] extra",
    )
    process_url.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        metavar="N",
        help="exact speaker count when known — the single best quality lever",
    )

    gc = sub.add_parser("gc", help="delete old jobs from the local store")
    gc.add_argument("--keep-days", type=int, default=30, help="keep jobs newer than N days")

    return parser


def _cmd_serve() -> int:
    from .server import mcp

    # One line per start in the client's MCP log: which server, which extras
    # (a config that launches the minimal server still advertises process_url).
    logger.info("%s", version_line())
    mcp.run()
    return 0


def _print_human_summary(summary: dict[str, object]) -> None:
    transcript = summary["transcript"]
    frames = summary["frames"]
    ocr = summary["ocr"]
    assert isinstance(transcript, dict) and isinstance(frames, dict) and isinstance(ocr, dict)
    wall = summary["wall_clock"]
    wall_line = "unknown (t_ms only)"
    if isinstance(wall, dict):
        wall_line = f"{wall['start_utc']} (source={wall['source']}, {wall['confidence']})"
    media = summary["media"]
    assert isinstance(media, dict)
    reused_note = "  (reused existing result)" if summary["reused"] else ""
    print(f"job_id     : {summary['job_id']}{reused_note}")
    print(f"media      : {media['filename']} [{media['kind']}] {media['duration_s']}s")
    print(f"wall clock : {wall_line}")
    print(
        f"transcript : {transcript['segment_count']} segments"
        f" (language={transcript['language']}, model={transcript['model']})"
    )
    print(f"frames     : {frames['unique_count']} unique / {frames['count']} total")
    ocr_text_count = ocr["unique_frames_with_text"]
    print(f"ocr        : enabled={ocr['enabled']} frames_with_text={ocr_text_count}")
    diarization = summary.get("diarization")
    if isinstance(diarization, dict):
        if diarization["available"]:
            roster = ", ".join(
                f"{speaker['label']} {speaker['talk_time_ms'] / 1000:.0f}s"
                for speaker in diarization["speakers"]
            )
            amended = "  (amended existing job)" if summary.get("diarization_amended") else ""
            print(
                f"speakers   : {diarization['detected_num_speakers']} ({roster}){amended}"
            )
        else:
            print(f"speakers   : unavailable — {diarization['reason']}")
    print(f"elapsed    : {summary['elapsed_s']}s")
    preview = transcript["preview_segments"]
    if isinstance(preview, list) and preview:
        print("preview    :")
        for segment in preview[:5]:
            assert isinstance(segment, dict)
            prefix = f"{segment['speaker']} " if segment.get("speaker") else ""
            print(f"  [{segment['t_ms']:>7} ms] {prefix}{segment['text']}")
        if transcript["preview_truncated"] or len(preview) > 5:
            print("  … (use get_transcript for the rest)")


def _cmd_process(args: argparse.Namespace) -> int:
    from .core import pipeline

    def on_progress(stage: str, fraction: float) -> None:
        print(f"[{fraction * 100:5.1f}%] {stage}", file=sys.stderr)

    result = pipeline.process_media(
        args.file,
        recorded_at=args.recorded_at,
        vocabulary=args.vocabulary,
        language=args.language,
        model=args.model,
        diarize_speakers=True if args.diarize else None,
        num_speakers=args.num_speakers,
        force=args.force,
        progress=on_progress,
    )
    summary = pipeline.summarize(result)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human_summary(summary)
    return 0


def _cmd_process_url(args: argparse.Namespace) -> int:
    from .core import pipeline, url_ingest

    def on_progress(stage: str, fraction: float) -> None:
        # progress goes to stderr; stdout stays machine-readable for --json
        print(f"[{fraction * 100:5.1f}%] {stage}", file=sys.stderr)

    ingested = url_ingest.process_url(
        args.url,
        refresh=args.refresh,
        force=args.force,
        recorded_at=args.recorded_at,
        vocabulary=args.vocabulary,
        language=args.language,
        model=args.model,
        diarize_speakers=True if args.diarize else None,
        num_speakers=args.num_speakers,
        progress=on_progress,
    )
    summary = pipeline.summarize(ingested.result)
    origin_block = dict(summary.get("origin") or {})
    origin_block["reused_url_mapping"] = ingested.reused_url_mapping
    origin_block["refreshed"] = ingested.refreshed
    summary["origin"] = origin_block
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human_summary(summary)
        origin = summary["origin"]
        label = origin.get("provider_id") or origin.get("host") or origin.get("provider")
        reused = "  (reused stored job, no network)" if ingested.reused_url_mapping else ""
        print(f"origin     : {origin['kind']} {label}{reused}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    from .core import jobs

    result = jobs.gc(keep_days=args.keep_days)
    if result.removed:
        print(f"removed {len(result.removed)} job(s): {', '.join(result.removed)}")
    if result.recovered:
        print(
            f"recovered {len(result.recovered)} interrupted rebuild(s): "
            + ", ".join(f"{item.staging} ({item.action})" for item in result.recovered)
        )
    if result.swept:
        print(
            f"swept {len(result.swept)} partial/staging dir(s): "
            f"{', '.join(result.swept)}"
        )
    if not result.removed and not result.swept and not result.recovered:
        print(f"nothing to remove (keep-days={args.keep_days})")
    return 0


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(effective_argv)
    except UsageError as exc:
        from .core.url_ingest import redact

        message = redact(str(exc))
        exc.parser.print_usage(sys.stderr)
        print(f"{exc.parser.prog}: error: {message}", file=sys.stderr)
        if "--json" in effective_argv:
            document = {"error": {"type": type(exc).__name__, "message": message}}
            print(json.dumps(document, ensure_ascii=False, indent=2))
        raise SystemExit(2) from None
    try:
        if args.command == "process":
            code = _cmd_process(args)
        elif args.command == "process-url":
            code = _cmd_process_url(args)
        elif args.command == "gc":
            code = _cmd_gc(args)
        else:  # "serve" or no subcommand
            code = _cmd_serve()
    except TalkthroughError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if getattr(args, "json", False):
            # the machine-readable contract holds on failure too: exit 2, the
            # human line on stderr, one JSON document on stdout
            document = {"error": {"type": type(exc).__name__, "message": str(exc)}}
            print(json.dumps(document, ensure_ascii=False, indent=2))
        code = 2
    raise SystemExit(code)
