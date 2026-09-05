"""The CLI entry point: logging privacy for every command, ``--version``, and
the ``--json`` error document. The pipeline itself is covered elsewhere."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from talkthrough_mcp import cli

CANARY = "sig=SECRET-TOKEN-4242"


@pytest.fixture
def restore_http_loggers() -> Iterator[None]:
    """``_configure_logging`` sets process-global logger levels; undo them."""
    loggers = [logging.getLogger(name) for name in cli._HTTP_CLIENT_LOGGERS]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def _record(
    name: str, msg: str, args: tuple[object, ...], level: int = logging.INFO
) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


# --- log privacy (0.4.1: httpx printed the pinned URL, query included, at INFO) ---


def test_privacy_handler_redacts_foreign_records_and_keeps_own_labels() -> None:
    stream = io.StringIO()
    handler = cli._privacy_handler(stream)
    handler.handle(
        _record(
            "httpx",
            'HTTP Request: %s %s "%s"',
            ("GET", f"https://203.0.113.10/clip.mp4?{CANARY}", "HTTP/1.1 200 OK"),
        )
    )
    handler.handle(
        _record(
            "talkthrough_mcp.core.url_ingest",
            "%s already ingested as job %s — no network",
            ("https://cdn.example.com/…", "abcdef0123456789"),
        )
    )
    text = stream.getvalue()
    assert 'httpx INFO HTTP Request: GET <url> "HTTP/1.1 200 OK"' in text
    assert CANARY not in text and "203.0.113.10" not in text
    # a safe label is the point of the own log line: it survives untouched
    assert "https://cdn.example.com/… already ingested as job abcdef0123456789" in text


def test_privacy_handler_scrubs_tracebacks_from_any_logger() -> None:
    stream = io.StringIO()
    handler = cli._privacy_handler(stream)
    try:
        raise RuntimeError(f"boom while fetching https://cdn.example.com/x.mp4?{CANARY}")
    except RuntimeError:
        record = logging.LogRecord(
            "talkthrough_mcp.core.jobs", logging.WARNING, __file__, 1, "sweep failed", None,
            sys.exc_info(),
        )
    handler.handle(record)
    text = stream.getvalue()
    assert "sweep failed" in text and "RuntimeError: boom while fetching <url>" in text
    assert CANARY not in text


def test_configure_logging_holds_the_http_client_loggers_at_warning(
    restore_http_loggers: None,
) -> None:
    for name in cli._HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    cli._configure_logging()
    for name in cli._HTTP_CLIENT_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    root = logging.getLogger()
    assert root.handlers, "the root logger must end up with a handler"
    assert all(
        any(isinstance(item, cli._RedactUrls) for item in handler.filters)
        for handler in root.handlers
    )
    cli._configure_logging()  # idempotent: no duplicate filters
    assert all(
        sum(isinstance(item, cli._RedactUrls) for item in handler.filters) == 1
        for handler in root.handlers
    )


def test_main_configures_logging_before_dispatching(
    isolated_home: object, capsys: pytest.CaptureFixture[str], restore_http_loggers: None
) -> None:
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    with pytest.raises(SystemExit) as info:
        cli.main(["gc"])
    assert info.value.code == 0
    assert "nothing to remove" in capsys.readouterr().out
    assert logging.getLogger("httpx").level == logging.WARNING


# --- --version and the --json error document (0.4.1) -------------------------------


def test_version_flag_names_the_package_python_and_extras(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from talkthrough_mcp import __version__

    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"talkthrough-mcp {__version__} (python 3.")
    assert "url extra: yt-dlp " in out and "diarization extra: sherpa-onnx " in out
    assert out == cli.version_line() + "\n"


def test_version_line_says_which_extra_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata as metadata

    real = metadata.version

    def without_yt_dlp(name: str) -> str:
        if name == "yt-dlp":
            raise metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(metadata, "version", without_yt_dlp)
    line = cli.version_line()
    assert "url extra: not installed (direct https:// media links only)" in line
    assert "diarization extra: sherpa-onnx " in line


def test_json_flag_turns_a_failure_into_an_error_document(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    playlist = "https://www.youtube.com/playlist?list=PLxyz"
    with pytest.raises(SystemExit) as info:
        cli.main(["process-url", playlist, "--json"])
    assert info.value.code == 2
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert set(document) == {"error"} and set(document["error"]) == {"type", "message"}
    assert document["error"]["type"] == "UnsupportedUrlError"
    assert "playlist" in document["error"]["message"]
    error_lines = [line for line in captured.err.splitlines() if line.startswith("error: ")]
    assert len(error_lines) == 1 and "playlist" in error_lines[0]

    with pytest.raises(SystemExit) as info:
        cli.main(["process", str(isolated_home / "missing.mp4"), "--json"])
    assert info.value.code == 2
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["type"] == "ValidationError"
    assert "file not found" in document["error"]["message"]

    with pytest.raises(SystemExit) as info:
        cli.main(["process-url", playlist])  # without --json stdout stays empty
    assert info.value.code == 2 and capsys.readouterr().out == ""


def test_serve_logs_its_version_and_extras_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, restore_http_loggers: None
) -> None:
    from talkthrough_mcp import server

    monkeypatch.setattr(server.mcp, "run", lambda: None)
    with (
        caplog.at_level(logging.INFO, logger="talkthrough_mcp.cli"),
        pytest.raises(SystemExit) as info,
    ):
        cli.main(["serve"])
    assert info.value.code == 0
    assert cli.version_line() in caplog.text
