#!/usr/bin/env python3
"""Initialize the local stdio server and verify its public inventory."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from talkthrough_mcp import __version__, guidance


async def check() -> None:
    with tempfile.TemporaryDirectory(prefix="talkthrough-inventory-") as home:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "talkthrough_mcp.cli", "serve"],
            env={**os.environ, "TALKTHROUGH_HOME": home},
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            prompts = await session.list_prompts()
            assert initialized.server_info.version == __version__
            assert sorted(tool.name for tool in tools.tools) == sorted(guidance.TOOL_NAMES)
            assert sorted(prompt.name for prompt in prompts.prompts) == sorted(
                guidance.PROMPT_NAMES
            )
            print(
                f"talkthrough-mcp {__version__}: "
                f"{len(tools.tools)} tools, {len(prompts.prompts)} prompts"
            )


if __name__ == "__main__":
    asyncio.run(check())
