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


@pytest.mark.parametrize("name", guidance.PROMPT_NAMES)
def test_example_prompt_files_do_not_drift(name: str) -> None:
    path = REPO_ROOT / "examples" / "prompts" / f"{name}.md"
    assert path.is_file(), f"missing {path} — examples/prompts must mirror server prompts"
    assert path.read_text(encoding="utf-8") == guidance.render_prompt(name, "<job_id>"), (
        f"{path} drifted from guidance.PROMPT_TEMPLATES[{name!r}] — regenerate it"
    )
