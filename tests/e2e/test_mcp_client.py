"""E2E over the real MCP stdio transport — the tool surface exactly as a client sees it.

Spawns the server with ``uv run talkthrough-mcp serve`` (fresh TALKTHROUGH_HOME,
whisper ``tiny``), then exercises the full loop: tool discovery with guidance
examples on the wire, prompt discovery + rendering, processing the committed
fixture, moment retrieval with real image content, search with wall-clock, and
SRT export.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from tests.integration.fixture_facts import DEMO_MP4

from talkthrough_mcp import guidance

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESS_TIMEOUT = timedelta(seconds=600)


def _server_params(home: Path) -> StdioServerParameters:
    env = {
        **os.environ,
        "TALKTHROUGH_HOME": str(home),
        "TALKTHROUGH_WHISPER_MODEL": "tiny",
    }
    return StdioServerParameters(
        command="uv",
        args=["run", "--no-sync", "--directory", str(REPO_ROOT), "talkthrough-mcp", "serve"],
        env=env,
        cwd=str(REPO_ROOT),
    )


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.isError, f"tool errored: {result.content}"
    if isinstance(result.structuredContent, dict) and result.structuredContent:
        candidate = result.structuredContent
        return candidate.get("result", candidate) if "result" in candidate else candidate
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    loaded = json.loads(first.text)
    assert isinstance(loaded, dict)
    return loaded


async def _run_session(home: Path) -> None:
    async with (
        stdio_client(_server_params(home)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        # 1. Tool discovery: 7 tools, schemas, guidance examples ON THE WIRE.
        tools_result = await session.list_tools()
        tools = {tool.name: tool for tool in tools_result.tools}
        assert sorted(tools) == sorted(guidance.TOOL_NAMES), sorted(tools)
        for name, tool in tools.items():
            assert tool.inputSchema and tool.inputSchema.get("type") == "object", name
            lines = guidance.example_lines(tool.description or "")
            assert len(lines) >= 10, f"{name}: only {len(lines)} example lines over the wire"

        # 2. Prompt discovery + rendering.
        prompts_result = await session.list_prompts()
        prompt_names = sorted(prompt.name for prompt in prompts_result.prompts)
        assert prompt_names == sorted(guidance.PROMPT_NAMES)

        # 3. Process the committed fixture (the long call).
        process_result = await session.call_tool(
            "process_media",
            {"path": str(DEMO_MP4)},
            read_timeout_seconds=PROCESS_TIMEOUT,
        )
        summary = _payload(process_result)
        job_id = summary["job_id"]
        assert summary["transcript"]["segment_count"] >= 1
        assert summary["frames"]["unique_count"] >= 3
        assert summary["wall_clock"]["source"] == "metadata"
        assert summary["transcript"]["preview_segments"], "summary must carry a preview"

        # 3b. Prompt renders non-empty for the real job and names its tools.
        prompt = await session.get_prompt("triage-recording", {"job_id": job_id})
        assert prompt.messages, "triage-recording rendered no messages"
        prompt_text = prompt.messages[0].content
        assert isinstance(prompt_text, types.TextContent)
        assert job_id in prompt_text.text
        for tool_name in ("get_moment", "search", "get_transcript"):
            assert tool_name in prompt_text.text

        # 4. get_moment around scene 2: image content + transcript text.
        moment_result = await session.call_tool(
            "get_moment", {"job_id": job_id, "start_ms": 5000, "end_ms": 9000}
        )
        assert not moment_result.isError
        image_blocks = [
            block for block in moment_result.content if isinstance(block, types.ImageContent)
        ]
        text_blocks = [
            block for block in moment_result.content if isinstance(block, types.TextContent)
        ]
        assert len(image_blocks) >= 1, "moment must return at least one image content block"
        assert image_blocks[0].mimeType.startswith("image/")
        assert len(image_blocks[0].data) > 1000, "image payload suspiciously small"
        moment_meta = json.loads(text_blocks[0].text)
        assert moment_meta["transcript"], "moment must include transcript text"

        # 5. search("login") → hit with wall-clock time.
        search_result = await session.call_tool("search", {"job_id": job_id, "query": "login"})
        search_payload = _payload(search_result)
        assert search_payload["hit_count"] >= 1
        assert any(hit["t_wall"] for hit in search_payload["hits"]), (
            "search hits must carry t_wall when the wall clock is known"
        )

        # 6. SRT export is well-formed.
        srt_result = await session.call_tool(
            "get_transcript", {"job_id": job_id, "format": "srt"}
        )
        srt_payload = _payload(srt_result)
        srt = srt_payload["srt"]
        assert srt.startswith("1\n00:00:0")
        assert " --> " in srt

        # 7. list_jobs sees the processed job.
        jobs_result = await session.call_tool("list_jobs", {})
        jobs_payload = _payload(jobs_result)
        assert any(job["job_id"] == job_id for job in jobs_payload["jobs"])


@pytest.mark.timeout(900)
def test_mcp_stdio_end_to_end(tmp_path: Path) -> None:
    asyncio.run(_run_session(tmp_path / "talkthrough-home"))
