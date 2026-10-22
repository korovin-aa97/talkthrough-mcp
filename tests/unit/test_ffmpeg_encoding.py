"""FFmpeg pipes use UTF-8 even when Python defaults to a Windows code page."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from talkthrough_mcp.core import probe
from talkthrough_mcp.core.errors import ToolFailureError
from talkthrough_mcp.core.ffmpeg import run_tool


@pytest.mark.parametrize("code_page", ["cp1251", "cp1252"])
def test_tool_preserves_utf8_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, code_page: str
) -> None:
    # Override the subprocess default even when CI runs with PYTHONUTF8=1.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: code_page)
    text = "\u0418\u0418 \u0432 NDBC \u2014 \u4f1a\u8bae"
    script = (
        f"import sys; sys.stdout.buffer.write({text.encode()!r}); "
        f"sys.stderr.buffer.write({text.encode()!r})"
    )

    result = run_tool([sys.executable, "-c", script], timeout=30)

    assert result.stdout == text
    assert result.stderr == text


def test_tool_failure_keeps_utf8_diagnostics_with_non_utf8_noise() -> None:
    text = "\u0418\u0418.webm: cannot open file"
    message = text.encode() + b"\xff"
    script = f"import sys; sys.stderr.buffer.write({message!r}); sys.exit(1)"

    with pytest.raises(ToolFailureError) as info:
        run_tool([sys.executable, "-c", script], timeout=30)

    assert text in str(info.value)
    assert "\ufffd" in str(info.value)


@pytest.mark.parametrize("stdout", [None, "", " \r\n"])
def test_probe_reports_missing_output_as_a_tool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str | None
) -> None:
    media = tmp_path / "recording.webm"
    media.touch()
    result = subprocess.CompletedProcess(["ffprobe"], 0, stdout, "")
    monkeypatch.setattr(probe, "run_tool", lambda *args, **kwargs: result)

    with pytest.raises(ToolFailureError, match="ffprobe returned empty output"):
        probe.probe_media(media)


def test_probe_preserves_unicode_metadata_under_cp1251(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "\u0418\u0418 \u0432 NDBC.webm"
    media.touch()
    payload = json.dumps(
        {
            "streams": [{"codec_type": "audio"}],
            "format": {"duration": "10", "tags": {"title": media.stem}},
        },
        ensure_ascii=False,
    ).encode()
    script = f"import sys; sys.stdout.buffer.write({payload!r})"
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1251")
    monkeypatch.setattr(
        probe,
        "run_tool",
        lambda *args, **kwargs: run_tool([sys.executable, "-c", script], timeout=30),
    )

    info = probe.probe_media(media)

    assert info.has_audio
    assert info.format_tags["title"] == media.stem
