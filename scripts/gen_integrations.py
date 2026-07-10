#!/usr/bin/env python3
"""Single source of truth for every engine integration artifact.

Run after changing guidance.py, the canonical skill, the example agent, or
INSTALL_PHASE:

    uv run python scripts/gen_integrations.py

NEVER hand-edit the generated files (the full list is what this script
prints); `tests/unit/test_guidance.py::test_generated_artifacts_do_not_drift`
byte-pins all of them to this generator.

Flip-day note: switching distribution from the git checkout to PyPI is ONE
change — set INSTALL_PHASE = "pypi" and re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from talkthrough_mcp import guidance  # noqa: E402

# --- the one flip-day switch -------------------------------------------------

INSTALL_PHASE = "git"  # "git" (private/pre-PyPI) | "pypi"

_UVX_ARGS = {
    "git": ["--from", "git+https://github.com/korovin-aa97/talkthrough-mcp", "talkthrough-mcp"],
    "pypi": ["talkthrough-mcp"],
}
UVX_ARGS: list[str] = _UVX_ARGS[INSTALL_PHASE]
UVX_CMDLINE = "uvx " + " ".join(UVX_ARGS)

ENV_DOC = (
    "Optional env vars: TALKTHROUGH_WHISPER_MODEL (default `small`; use `medium`/"
    "`large-v3` for non-English narration), TALKTHROUGH_OCR (`off` to disable), "
    "TALKTHROUGH_HOME (job store root, default `~/.talkthrough`)."
)

VERIFY_DOC = (
    "Verify: the client should list 7 tools (process_media, get_transcript, get_frames, "
    "get_moment, search, extract_frame, list_jobs). A `list_jobs` call returning an empty "
    "list is a healthy first run."
)

# --- helpers ------------------------------------------------------------------


def _jsonc(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"


def _engine_readme(
    title: str,
    config_path: str,
    snippet_lang: str,
    snippet: str,
    extra: str = "",
    docs_url: str | None = None,
) -> str:
    parts = [
        f"# {title}\n",
        "\nServer command (stdio): `" + UVX_CMDLINE + "`\n",
        f"\nConfig: `{config_path}`\n",
        f"\n```{snippet_lang}\n{snippet}```\n",
    ]
    if extra:
        parts.append("\n" + extra.rstrip() + "\n")
    parts.append("\n" + ENV_DOC + "\n")
    parts.append("\n" + VERIFY_DOC + "\n")
    if docs_url:
        parts.append(f"\nEngine docs: <{docs_url}>\n")
    parts.append(
        "\nAgent-followable install steps for any client: [`llms-install.md`](../../llms-install.md).\n"
    )
    return "".join(parts)


def _mcp_servers_json(command: str, args: list[str]) -> str:
    return _jsonc({"mcpServers": {"talkthrough": {"command": command, "args": args}}})


# --- artifact builders ---------------------------------------------------------


def build_claude_commands() -> dict[str, str]:
    preamble = (
        "If no job_id was passed as an argument: call list_jobs() to find the "
        "right recording, or process_media(path) if the user gave a file path — "
        "then follow the workflow below with that job_id.\n\n"
    )
    artifacts: dict[str, str] = {}
    for name in guidance.PROMPT_NAMES:
        body = guidance.render_prompt(name, "$ARGUMENTS")
        desc = guidance.PROMPT_DESCRIPTIONS[name].replace('"', "'")
        artifacts[f"integrations/claude-code/commands/{name}.md"] = (
            f'---\ndescription: "{desc}"\n---\n\n{preamble}{body}'
        )
    return artifacts


def build_example_prompts() -> dict[str, str]:
    return {
        f"examples/prompts/{name}.md": guidance.render_prompt(name, "<job_id>")
        for name in guidance.PROMPT_NAMES
    }


def build_skill_mirrors() -> dict[str, str]:
    canonical = (REPO / ".agents" / "skills" / "talkthrough" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    return {"integrations/claude-code/skills/talkthrough/SKILL.md": canonical}


def build_agent_mirrors() -> dict[str, str]:
    canonical = (REPO / "examples" / "agents" / "feedback-triage.md").read_text(encoding="utf-8")
    return {"integrations/claude-code/agents/feedback-triage.md": canonical}


def build_mcp_configs() -> dict[str, str]:
    return {
        # Plugin config: distribution form (uvx).
        "integrations/claude-code/.mcp.json": _jsonc(
            {"mcpServers": {"talkthrough": {"command": "uvx", "args": UVX_ARGS}}}
        ),
        # Checkout config: contributors get the LOCAL server when opening this repo.
        ".mcp.json": _jsonc(
            {
                "mcpServers": {
                    "talkthrough-dev": {
                        "command": "uv",
                        "args": ["run", "--directory", ".", "talkthrough-mcp"],
                    }
                }
            }
        ),
    }


def build_engine_docs() -> dict[str, str]:
    artifacts: dict[str, str] = {}

    artifacts["integrations/codex/README.md"] = _engine_readme(
        "OpenAI Codex CLI",
        "~/.codex/config.toml (or project-scoped .codex/config.toml in trusted projects)",
        "toml",
        (
            "[mcp_servers.talkthrough]\n"
            'command = "uvx"\n'
            f"args = {json.dumps(UVX_ARGS)}\n"
        ),
        extra=(
            "Skills: this repo ships the talkthrough skill at `.agents/skills/talkthrough/` — "
            "Codex discovers it automatically inside a checkout; for global use copy it to "
            "`~/.agents/skills/` and invoke with `$talkthrough`."
        ),
        docs_url="https://developers.openai.com/codex/mcp",
    )

    artifacts["integrations/openclaw/README.md"] = _engine_readme(
        "OpenClaw",
        "~/.openclaw/openclaw.json",
        "json",
        _jsonc(
            {
                "mcp": {
                    "servers": {
                        "talkthrough": {"command": "uvx", "args": UVX_ARGS}
                    }
                }
            }
        ),
        extra=(
            "Or via CLI:\n\n```bash\nopenclaw mcp add talkthrough --command uvx "
            + " ".join(f"--arg {arg}" for arg in UVX_ARGS)
            + "\n```\n\nClawHub: a publish-ready skill wrapper lives in "
            "[`clawhub/`](clawhub/) (submit after the repo is public)."
        ),
        docs_url="https://docs.openclaw.ai/cli/mcp",
    )

    artifacts["integrations/gemini-cli/README.md"] = _engine_readme(
        "Gemini CLI",
        "~/.gemini/settings.json",
        "json",
        _mcp_servers_json("uvx", UVX_ARGS),
        docs_url="https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html",
    )

    artifacts["integrations/cursor/README.md"] = _engine_readme(
        "Cursor",
        "~/.cursor/mcp.json (or project .cursor/mcp.json)",
        "json",
        _mcp_servers_json("uvx", UVX_ARGS),
        docs_url="https://cursor.com/docs/context/mcp",
    )

    artifacts["integrations/cline/README.md"] = _engine_readme(
        "Cline / Roo Code",
        "cline_mcp_settings.json (via MCP Servers UI)",
        "json",
        _mcp_servers_json("uvx", UVX_ARGS),
        extra=(
            "Fastest path: ask Cline to install it — point it at "
            "[`llms-install.md`](../../llms-install.md)."
        ),
        docs_url="https://docs.cline.bot/mcp/configuring-mcp-servers",
    )

    artifacts["integrations/opencode/README.md"] = _engine_readme(
        "OpenCode",
        "opencode.json (project) or ~/.config/opencode/opencode.json",
        "json",
        _jsonc(
            {
                "mcp": {
                    "talkthrough": {
                        "type": "local",
                        "command": ["uvx", *UVX_ARGS],
                        "enabled": True,
                    }
                }
            }
        ),
        docs_url="https://opencode.ai/docs/mcp-servers",
    )

    artifacts["integrations/goose/README.md"] = _engine_readme(
        "Goose",
        "~/.config/goose/config.yaml",
        "yaml",
        (
            "extensions:\n"
            "  talkthrough:\n"
            "    enabled: true\n"
            "    type: stdio\n"
            "    cmd: uvx\n"
            f"    args: {json.dumps(UVX_ARGS)}\n"
        ),
        docs_url="https://block.github.io/goose/docs/getting-started/using-extensions",
    )

    artifacts["integrations/copilot-cli/README.md"] = _engine_readme(
        "GitHub Copilot CLI",
        "~/.copilot/mcp-config.json",
        "json",
        _mcp_servers_json("uvx", UVX_ARGS),
        docs_url="https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli",
    )

    artifacts["integrations/windsurf/README.md"] = _engine_readme(
        "Windsurf",
        "~/.codeium/windsurf/mcp_config.json",
        "json",
        _mcp_servers_json("uvx", UVX_ARGS),
        docs_url="https://docs.windsurf.com/windsurf/cascade/mcp",
    )

    artifacts["integrations/zed/README.md"] = _engine_readme(
        "Zed",
        "settings.json (Zed)",
        "json",
        _jsonc(
            {
                "context_servers": {
                    "talkthrough": {
                        "source": "custom",
                        "command": {"path": "uvx", "args": UVX_ARGS},
                    }
                }
            }
        ),
        docs_url="https://zed.dev/docs/ai/mcp",
    )

    return artifacts


def build_clawhub_skill() -> dict[str, str]:
    content = f"""---
name: talkthrough
description: Analyze narrated screen recordings and audio files — timestamped transcript, scene keyframes, OCR text, and wall-clock anchoring via the local talkthrough MCP server. Use when the user shares a recording or asks to triage feedback, extract meeting actions, or correlate a recording with logs.
---

# talkthrough for OpenClaw

This skill wires the talkthrough MCP server into OpenClaw and teaches the
workflow. Everything runs locally: recordings never leave the machine.

## Setup (once)

Add the MCP server:

```bash
openclaw mcp add talkthrough --command uvx {" ".join(f"--arg {arg}" for arg in UVX_ARGS)}
```

Requires `uv` (https://astral.sh/uv). First processing downloads a whisper
model once (~460 MB for the default `small`).

## Workflow

1. `process_media(path)` — idempotent by content hash; returns a compact
   summary with job_id (re-calls on the same file are instant).
2. `get_transcript(job_id)` / `search(job_id, "<word>")` — orient; search
   covers speech AND on-screen OCR text.
3. `get_moment(job_id, t0-2000, t1+2000)` — evidence bundle per remark:
   transcript slice + up to 3 frames + OCR + wall-clock range.
4. `extract_frame(job_id, at_ms, crop=...)` — exact instant, native
   resolution, when keyframes miss the moment.
5. `list_jobs()` — recordings processed earlier remain queryable.

Timestamps: `t_ms` is video-relative; `t_wall` is real wall-clock time when
the recording start is known — use it to correlate remarks with logs
(±30 s window). Audio-only files (.m4a/.mp3/…) have transcripts but no
frames; frame tools erroring on them is expected.

Full docs: https://github.com/korovin-aa97/talkthrough-mcp
"""
    return {"integrations/openclaw/clawhub/SKILL.md": content}


def build_integrations_index() -> dict[str, str]:
    content = f"""# Integrations

The talkthrough MCP server is engine-agnostic (stdio MCP). Server command:

```
{UVX_CMDLINE}
```

One folder per engine with the exact config to paste:

| Engine | Folder | Extras beyond the MCP config |
|---|---|---|
| Claude Code | [`claude-code/`](claude-code/) | full plugin: 5 slash commands, triage agent, skill |
| Claude Desktop | [`claude-desktop/`](claude-desktop/) | one-click `.mcpb` extension (draft) |
| OpenAI Codex CLI | [`codex/`](codex/) | `$talkthrough` skill via `.agents/skills/` |
| OpenClaw | [`openclaw/`](openclaw/) | ClawHub-ready skill wrapper |
| Gemini CLI | [`gemini-cli/`](gemini-cli/) | — |
| Cursor | [`cursor/`](cursor/) | — |
| Cline / Roo Code | [`cline/`](cline/) | agent self-install via `llms-install.md` |
| OpenCode | [`opencode/`](opencode/) | — |
| Goose | [`goose/`](goose/) | — |
| GitHub Copilot CLI | [`copilot-cli/`](copilot-cli/) | — |
| Windsurf | [`windsurf/`](windsurf/) | — |
| Zed | [`zed/`](zed/) | — |

Anything else that speaks MCP over stdio works with the same command. The
cross-engine [Agent Skill](https://agentskills.io) lives at
[`.agents/skills/talkthrough/`](../.agents/skills/talkthrough/) (Claude Code,
Codex, Cursor, Copilot, Gemini CLI, Goose and others read this format).

Config snippets are generated by `scripts/gen_integrations.py` from one
source of truth — do not hand-edit them. Snippet shapes verified against
engine docs 2026-07; each engine README links the authoritative docs.
"""
    return {"integrations/README.md": content}


def build_artifacts() -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for builder in (
        build_claude_commands,
        build_example_prompts,
        build_skill_mirrors,
        build_agent_mirrors,
        build_mcp_configs,
        build_engine_docs,
        build_clawhub_skill,
        build_integrations_index,
    ):
        artifacts.update(builder())
    return artifacts


def main() -> None:
    for rel_path, content in sorted(build_artifacts().items()):
        path = REPO / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(rel_path)


if __name__ == "__main__":
    main()
