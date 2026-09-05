"""The CLI entry point: logging privacy for every command, ``--version``, and
the ``--json`` error document. The pipeline itself is covered elsewhere."""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator

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
