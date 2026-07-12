"""Guidance-layer quality gate.

Enforced contract: every tool description carries >=10 one-line examples
(each <=120 chars) plus a "when NOT to use" line; the server registers
exactly the 7 described tools and exactly the 5 workflow prompts; prompt
templates render non-empty, name the tools they orchestrate, and are
byte-identical with the files in ``examples/prompts/`` (no drift).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from talkthrough_mcp import guidance
from talkthrough_mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parents[2]

MIN_EXAMPLES = 10
MAX_EXAMPLES = 15
MAX_EXAMPLE_CHARS = 120

# Which tools each workflow prompt must explicitly orchestrate.
PROMPT_REQUIRED_TOOLS = {
    "triage-recording": {"get_transcript", "get_moment", "search", "extract_frame", "list_jobs"},
    "spec-from-workshop": {"get_transcript", "get_frames", "get_moment", "search"},
    "backlog-from-demo": {"get_transcript", "get_moment", "search"},
    "meeting-actions": {"get_transcript", "search", "get_moment"},
    "correlate-with-logs": {"list_jobs", "search", "get_moment", "process_media"},
}


@pytest.mark.parametrize("tool_name", guidance.TOOL_NAMES)
def test_every_tool_description_has_enough_short_examples(tool_name: str) -> None:
    description = guidance.TOOL_DESCRIPTIONS[tool_name]
    assert guidance.EXAMPLES_HEADER in description
    assert "When NOT to use" in description
    lines = guidance.example_lines(description)
    assert MIN_EXAMPLES <= len(lines) <= MAX_EXAMPLES, (
        f"{tool_name}: {len(lines)} examples, want {MIN_EXAMPLES}-{MAX_EXAMPLES}"
    )
    too_long = [line for line in lines if len(line) > MAX_EXAMPLE_CHARS]
    assert not too_long, f"{tool_name}: examples over {MAX_EXAMPLE_CHARS} chars: {too_long}"


def test_registered_tools_match_guidance_exactly() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert sorted(tool.name for tool in tools) == sorted(guidance.TOOL_NAMES)
    for tool in tools:
        assert tool.description == guidance.TOOL_DESCRIPTIONS[tool.name], (
            f"{tool.name}: registered description drifted from guidance.py"
        )
        assert len(guidance.example_lines(tool.description or "")) >= MIN_EXAMPLES


def test_every_tool_carries_honest_annotations() -> None:
    """Non-interactive clients (codex exec) silently cancel un-annotated tool
    calls — every tool must ship hints, and they must stay truthful."""
    writers = {"process_media", "extract_frame"}  # write only inside TALKTHROUGH_HOME
    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        ann = tool.annotations
        assert ann is not None, f"{tool.name}: missing ToolAnnotations"
        assert ann.destructiveHint is False
        assert ann.idempotentHint is True
        assert ann.openWorldHint is False
        assert ann.readOnlyHint is (tool.name not in writers), tool.name


def test_exactly_five_prompts_registered() -> None:
    prompts = asyncio.run(mcp.list_prompts())
    assert sorted(prompt.name for prompt in prompts) == sorted(guidance.PROMPT_NAMES)
    assert len(prompts) == 5
    for prompt in prompts:
        assert prompt.description == guidance.PROMPT_DESCRIPTIONS[prompt.name]
        arg_names = [argument.name for argument in prompt.arguments or []]
        assert arg_names[0] == "job_id"


@pytest.mark.parametrize("name", guidance.PROMPT_NAMES)
def test_prompt_renders_and_names_its_tools(name: str) -> None:
    rendered = guidance.render_prompt(name, "job42job42job42j")
    assert rendered.strip()
    assert "job42job42job42j" in rendered
    for tool_name in PROMPT_REQUIRED_TOOLS[name]:
        assert tool_name in rendered, f"prompt {name} must orchestrate {tool_name}"


@pytest.mark.parametrize("name", guidance.PROMPT_NAMES)
def test_prompt_optional_context_is_injected(name: str) -> None:
    rendered = guidance.render_prompt(name, "<job_id>", "ACME Payments GmbH")
    assert "ACME Payments GmbH" in rendered


def _generated_artifacts() -> dict[str, str]:
    from scripts.gen_integrations import build_artifacts

    return build_artifacts()


def test_generated_artifacts_do_not_drift() -> None:
    """Every engine-integration artifact is byte-pinned to scripts/gen_integrations.py.

    Covers examples/prompts, the Claude Code plugin (commands, .mcp.json,
    skill + agent mirrors), every integrations/<engine>/ doc, the ClawHub
    skill, the MCP-registry server.json, and the repo-root dev .mcp.json.
    Regenerate with: `uv run python scripts/gen_integrations.py`.
    """
    artifacts = _generated_artifacts()
    assert len(artifacts) >= 20
    for rel_path, expected in sorted(artifacts.items()):
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"missing generated file {rel_path} — run gen_integrations.py"
        assert path.read_text(encoding="utf-8") == expected, (
            f"{rel_path} drifted from scripts/gen_integrations.py — regenerate, "
            "or move your edit into the generator/canonical source"
        )


def test_readme_install_region_is_generated() -> None:
    """The marked install block in README.md is spliced by the generator (no drift)."""
    from scripts.gen_integrations import spliced_readme

    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert text == spliced_readme(text), (
        "README.md install region drifted from scripts/gen_integrations.py — regenerate"
    )


def test_install_buttons_encode_the_pypi_command() -> None:
    """Flip-day deeplink buttons (INSTALL_PHASE='pypi') round-trip to `uvx talkthrough-mcp`."""
    import base64
    import json
    import re
    import urllib.parse

    from scripts.gen_integrations import _install_buttons

    block = _install_buttons(["talkthrough-mcp"])
    cursor = re.search(r"cursor\.com/en/install-mcp\?name=talkthrough&config=([^)]+)\)", block)
    assert cursor is not None
    decoded = json.loads(base64.b64decode(urllib.parse.unquote(cursor.group(1))))
    assert decoded == {"command": "uvx", "args": ["talkthrough-mcp"]}
    vscode = re.search(r"vscode\.dev/redirect/mcp/install\?name=talkthrough&config=([^)&]+)", block)
    assert vscode is not None
    assert json.loads(urllib.parse.unquote(vscode.group(1)))["args"] == ["talkthrough-mcp"]
    assert "quality=insiders" in block


def test_generator_covers_every_engine_folder() -> None:
    """No hand-made stragglers: every integrations/<engine>/ has generated docs."""
    artifacts = _generated_artifacts()
    generated_dirs = {
        rel.split("/")[1]
        for rel in artifacts
        if rel.startswith("integrations/") and rel.count("/") >= 2
    }
    on_disk = {
        entry.name
        for entry in (REPO_ROOT / "integrations").iterdir()
        if entry.is_dir()
    }
    assert on_disk == generated_dirs, (
        f"engine folders without generator coverage: {on_disk - generated_dirs}"
    )


def test_marketplace_points_at_the_plugin_subdir() -> None:
    import json

    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    sources = [plugin["source"] for plugin in manifest["plugins"]]
    assert sources == ["./integrations/claude-code"]
    assert (REPO_ROOT / "integrations/claude-code/.claude-plugin/plugin.json").is_file()
