"""ffmpeg/ffprobe resolution ladder and subprocess helpers.

Resolution order: system binaries via ``shutil.which`` first, else the
pip-installed ``static-ffmpeg`` bundle (downloaded once on first use). A
missing system ffmpeg is therefore never a hard failure — one-command
install is a core promise.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import sys
from functools import lru_cache

from .errors import ToolFailureError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _resolved_binaries() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    logger.info("system ffmpeg not found — falling back to static-ffmpeg (one-time download)")
    try:
        from static_ffmpeg import run as static_run

        # static-ffmpeg prints download progress to stdout; stdout belongs to
        # the MCP stdio transport, so route it to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            ffmpeg, ffprobe = static_run.get_or_fetch_platform_executables_else_raise()
    except Exception as exc:  # pragma: no cover - depends on network/platform
        raise ToolFailureError(
            "ffmpeg is not installed and the static-ffmpeg fallback failed: "
            f"{exc} — install ffmpeg (e.g. `brew install ffmpeg`) and retry"
        ) from exc
    return str(ffmpeg), str(ffprobe)


def ffmpeg_path() -> str:
    return _resolved_binaries()[0]


def ffprobe_path() -> str:
    return _resolved_binaries()[1]


def run_tool(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run an external tool, raising ToolFailureError with its stderr on failure."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or "").strip().splitlines()[-8:]
        raise ToolFailureError(
            f"{cmd[0]} failed (rc={exc.returncode}): " + " | ".join(stderr_tail)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolFailureError(f"{cmd[0]} timed out after {timeout}s") from exc


@lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    """First line of `ffmpeg -version`, for the manifest tool_versions block."""
    try:
        proc = run_tool([ffmpeg_path(), "-version"], timeout=30)
    except ToolFailureError:  # pragma: no cover - version probe is best-effort
        return "unknown"
    first_line = (proc.stdout or "").splitlines()[:1]
    return first_line[0].strip() if first_line else "unknown"
