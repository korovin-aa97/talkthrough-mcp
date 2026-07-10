"""CLI entry point: ``serve`` (default) | ``process <file>`` | ``gc``.

``process`` is the debug/batch path: it runs the same pipeline the MCP tool
uses and prints the summary, so long recordings can be pre-processed outside
an agent session and then queried by job_id (the store is content-addressed).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .core.errors import TalkthroughError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="talkthrough-mcp",
        description="Local-first MCP server for narrated screen recordings.",
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

    gc = sub.add_parser("gc", help="delete old jobs from the local store")
    gc.add_argument("--keep-days", type=int, default=30, help="keep jobs newer than N days")

    return parser


def _cmd_serve() -> int:
    from .server import mcp

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
    print(f"elapsed    : {summary['elapsed_s']}s")
    preview = transcript["preview_segments"]
    if isinstance(preview, list) and preview:
        print("preview    :")
        for segment in preview[:5]:
            assert isinstance(segment, dict)
            print(f"  [{segment['t_ms']:>7} ms] {segment['text']}")
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
        force=args.force,
        progress=on_progress,
    )
    summary = pipeline.summarize(result)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human_summary(summary)
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    from .core import jobs

    removed = jobs.gc(keep_days=args.keep_days)
    if removed:
        print(f"removed {len(removed)} job(s): {', '.join(removed)}")
    else:
        print(f"nothing to remove (keep-days={args.keep_days})")
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "process":
            code = _cmd_process(args)
        elif args.command == "gc":
            code = _cmd_gc(args)
        else:  # "serve" or no subcommand
            code = _cmd_serve()
    except TalkthroughError as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
