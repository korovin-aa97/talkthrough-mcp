"""Packaging and documentation guards for the MCP SDK 2.x port."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from talkthrough_mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_uses_the_tested_mcp_2x_line() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert "mcp>=2.1.1,<3" in dependencies
    assert isinstance(mcp, MCPServer)


def test_no_runtime_or_test_import_uses_removed_fastmcp_path() -> None:
    offenders: list[str] = []
    removed_import = "mcp.server." + "fastmcp"
    for root in (REPO_ROOT / "src", REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if removed_import in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_troubleshooting_removes_the_old_sdk_workaround_for_v030() -> None:
    text = (REPO_ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    prose = " ".join(text.split())
    assert "Talkthrough **0.3.0 and newer** use the MCP Python SDK 2.x" in prose
    assert "adds a real dependency constraint" in prose
    assert "must not remain in a 0.3.0 config" in prose
