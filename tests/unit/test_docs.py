"""Stable documentation contracts for operational failure modes."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_corporate_tls_docs_cover_the_first_static_ffmpeg_download() -> None:
    text = (REPO_ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "third-party `static-ffmpeg` package" in normalized
    assert "same `SSL_CERT_FILE` setting" in normalized
    assert "one-time ffmpeg download" in normalized
