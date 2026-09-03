"""Stable documentation contracts for operational failure modes."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _troubleshooting_contract(path: Path) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    return {
        "four_cold_stages": all(
            fact in normalized
            for fact in (
                "four separate stages",
                "isolated environment",
                "managed Python",
                "media/model assets",
                "Warm processing",
            )
        ),
        "tls_before_setup": (
            "Set `SSL_CERT_FILE` before either uv setup or the first media processing"
            in normalized
        ),
        "uv_trust_and_mirror": all(
            fact in normalized
            for fact in ("`UV_SYSTEM_CERTS=true`", "`UV_PYTHON_INSTALL_MIRROR`")
        ),
        "cache_scopes": all(
            fact in normalized
            for fact in (
                "`uv cache prune`",
                "`uv cache clean talkthrough-mcp`",
                "bare `uv cache clean` clears the entire uv cache",
                "`talkthrough-mcp gc --keep-days 30` cleans Talkthrough jobs",
            )
        ),
    }


def test_corporate_tls_docs_cover_the_first_static_ffmpeg_download() -> None:
    text = (REPO_ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "third-party `static-ffmpeg` package" in normalized
    assert "same `SSL_CERT_FILE` setting" in normalized
    assert "one-time ffmpeg download" in normalized


def test_troubleshooting_keeps_operational_semantics() -> None:
    contract = _troubleshooting_contract(REPO_ROOT / "docs" / "TROUBLESHOOTING.md")
    assert all(contract.values()), contract


def test_copy_edit_does_not_pin_marketing_prose(tmp_path: Path) -> None:
    source = (REPO_ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    copy = tmp_path / "TROUBLESHOOTING.md"
    copy.write_text(
        source.replace(
            "Short answers to the failure modes people actually hit.",
            "Practical answers for common operational failures.",
        ),
        encoding="utf-8",
    )
    assert all(_troubleshooting_contract(copy).values())


@pytest.mark.parametrize("removed_fact", _troubleshooting_contract(
    REPO_ROOT / "docs" / "TROUBLESHOOTING.md"
))
def test_removing_an_operational_fact_breaks_its_contract(
    tmp_path: Path, removed_fact: str
) -> None:
    source = (REPO_ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    replacements = {
        "four_cold_stages": ("four separate stages", "several setup stages"),
        "tls_before_setup": ("Set `SSL_CERT_FILE` before", "Configure certificates before"),
        "uv_trust_and_mirror": ("`UV_PYTHON_INSTALL_MIRROR`", "the organization mirror"),
        "cache_scopes": ("`uv cache clean talkthrough-mcp`", "the package cache command"),
    }
    old, new = replacements[removed_fact]
    copy = tmp_path / "TROUBLESHOOTING.md"
    copy.write_text(source.replace(old, new), encoding="utf-8")
    assert _troubleshooting_contract(copy)[removed_fact] is False
