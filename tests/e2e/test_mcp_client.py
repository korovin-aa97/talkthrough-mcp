"""E2E over the real MCP stdio transport — the tool surface exactly as a client sees it.

Spawns the server with ``uv run talkthrough-mcp serve`` (fresh TALKTHROUGH_HOME,
whisper ``tiny``), then exercises the full loop: tool discovery with guidance
examples on the wire, prompt discovery + rendering, processing the committed
fixture, moment retrieval with real image content, search with wall-clock,
SRT export, speaker diarization (or its actionable error without the extra),
and the absolute frame paths of issue #13.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from tests.conftest import make_manifest
from tests.integration.fixture_facts import DEMO_MP4, TWO_VOICE_M4A, TWO_VOICE_NUM_SPEAKERS

from talkthrough_mcp import __version__ as package_version
from talkthrough_mcp import guidance
from talkthrough_mcp.core import diarize
from talkthrough_mcp.core.diarize import (
    Diarization,
    PendingSpeakerReviewContext,
    Turn,
    attribute_segments,
    speaker_roster,
)
from talkthrough_mcp.core.manifest import save_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESS_TIMEOUT = 600.0


def _preseed_model_env() -> dict[str, str]:
    """Resolve engine models into the stable test cache; env paths for the server.

    Same cache the integration suite uses (actions/cache persists it in CI),
    so the spawned server process never downloads.
    """
    cache = os.environ.get(
        "TALKTHROUGH_TEST_MODEL_CACHE", str(Path.home() / ".cache" / "talkthrough-test-models")
    )
    saved = os.environ.get("TALKTHROUGH_HOME")
    os.environ["TALKTHROUGH_HOME"] = cache
    try:
        seg = diarize.ensure_model_file(
            diarize.SEGMENTATION_MODELS[diarize.DEFAULT_SEGMENTATION_MODEL]
        )
        emb = diarize.ensure_model_file(
            diarize.EMBEDDING_MODELS[diarize.DEFAULT_EMBEDDING_MODEL]
        )
    finally:
        if saved is None:
            os.environ.pop("TALKTHROUGH_HOME", None)
        else:
            os.environ["TALKTHROUGH_HOME"] = saved
    return {
        "TALKTHROUGH_DIARIZATION_SEG_MODEL": str(seg),
        "TALKTHROUGH_DIARIZATION_EMB_MODEL": str(emb),
    }


def _server_params(home: Path) -> StdioServerParameters:
    env = {
        **os.environ,
        "TALKTHROUGH_HOME": str(home),
        "TALKTHROUGH_WHISPER_MODEL": "tiny",
        # The SDK 2.x process call must stay clean when warnings are fatal.
        "PYTHONWARNINGS": "error",
    }
    env.pop("TALKTHROUGH_DIARIZE", None)  # keep the spawned server's defaults canonical
    if diarize.engine_available():
        env.update(_preseed_model_env())
    return StdioServerParameters(
        command="uv",
        args=["run", "--no-sync", "--directory", str(REPO_ROOT), "talkthrough-mcp", "serve"],
        env=env,
        cwd=str(REPO_ROOT),
    )


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.is_error, f"tool errored: {result.content}"
    if isinstance(result.structured_content, dict) and result.structured_content:
        candidate = result.structured_content
        return candidate.get("result", candidate) if "result" in candidate else candidate
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    loaded = json.loads(first.text)
    assert isinstance(loaded, dict)
    return loaded


async def _run_session(home: Path) -> None:
    diarized_job_id: str | None = None
    errlog = (home.parent / "talkthrough-server.stderr").open("w+", encoding="utf-8")
    async with (
        stdio_client(_server_params(home), errlog=errlog) as (read, write),
        # No logging_callback: this is the client-without-logging-capability path.
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.server_info.name == "talkthrough"
        assert initialized.server_info.version == package_version
        assert (
            initialized.instructions
            and "Local-first recording analysis" in initialized.instructions
        )

        # 1. Tool discovery: 8 tools, schemas, guidance examples ON THE WIRE.
        tools_result = await session.list_tools()
        tools = {tool.name: tool for tool in tools_result.tools}
        assert sorted(tools) == sorted(guidance.TOOL_NAMES), sorted(tools)
        for name, tool in tools.items():
            assert tool.input_schema and tool.input_schema.get("type") == "object", name
            assert tool.annotations is not None, name
            assert tool.annotations.destructive_hint is False, name
            lines = guidance.example_lines(tool.description or "")
            assert len(lines) >= 10, f"{name}: only {len(lines)} example lines over the wire"

        # 2. Prompt discovery + rendering.
        prompts_result = await session.list_prompts()
        prompt_names = sorted(prompt.name for prompt in prompts_result.prompts)
        assert prompt_names == sorted(guidance.PROMPT_NAMES)

        # 3. Process the committed fixture (the long call).
        progress_updates: list[tuple[float, float | None, str | None]] = []

        async def record_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            progress_updates.append((progress, total, message))

        process_result = await session.call_tool(
            "process_media",
            {"path": str(DEMO_MP4)},
            read_timeout_seconds=PROCESS_TIMEOUT,
            progress_callback=record_progress,
        )
        assert isinstance(process_result, types.CallToolResult)
        assert process_result.structured_content, "process_media must keep structured output"
        summary = _payload(process_result)
        job_id = summary["job_id"]
        assert summary["transcript"]["segment_count"] >= 1
        assert summary["frames"]["unique_count"] >= 3
        assert summary["wall_clock"]["source"] == "metadata"
        assert summary["transcript"]["preview_segments"], "summary must carry a preview"
        assert progress_updates, "process_media must report progress over MCP"
        assert progress_updates[0][2] == (
            f"processing {DEMO_MP4} (local pipeline: ffprobe → whisper → frames → OCR)"
        )
        assert progress_updates[-1][:2] == (1.0, 1.0)

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
        assert isinstance(moment_result, types.CallToolResult)
        assert not moment_result.is_error
        image_blocks = [
            block for block in moment_result.content if isinstance(block, types.ImageContent)
        ]
        text_blocks = [
            block for block in moment_result.content if isinstance(block, types.TextContent)
        ]
        assert len(image_blocks) >= 1, "moment must return at least one image content block"
        assert image_blocks[0].mime_type.startswith("image/")
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

        # 5b. v0.2.2: multi-word AND-search on the wire; speaker= on an
        # undiarized job answers honestly instead of erroring.
        multiword = _payload(
            await session.call_tool(
                "search", {"job_id": job_id, "query": "page login"}
            )
        )
        assert multiword["hit_count"] >= 1, "order-free multi-word query must hit"
        any_word = _payload(
            await session.call_tool(
                "search",
                {
                    "job_id": job_id,
                    "query": "login zzznonexistent",
                    "match_mode": "any_word",
                },
            )
        )
        assert any_word["match_mode"] == "any_word"
        assert any_word["hit_count"] >= 1
        undiarized_filter = _payload(
            await session.call_tool(
                "search", {"job_id": job_id, "query": "login", "speaker": "S1"}
            )
        )
        assert undiarized_filter["hits"] == []
        assert "not diarized" in undiarized_filter["note"]

        # 5c. v0.2.3: a zero-hit multi-word query explains the per-segment
        # matching instead of returning a mute empty list.
        zero_multi = _payload(
            await session.call_tool(
                "search", {"job_id": job_id, "query": "login zzznonexistent"}
            )
        )
        assert zero_multi["hit_count"] == 0
        assert "no single segment contains ALL the words" in zero_multi["note"]

        # 6. SRT export is well-formed; v0.2.2: the payload names the media kind.
        srt_result = await session.call_tool(
            "get_transcript", {"job_id": job_id, "format": "srt"}
        )
        srt_payload = _payload(srt_result)
        srt = srt_payload["srt"]
        assert srt.startswith("1\n00:00:0")
        assert " --> " in srt
        assert srt_payload["media_kind"] == "video"

        # 7. list_jobs sees the processed job.
        jobs_result = await session.call_tool("list_jobs", {})
        jobs_payload = _payload(jobs_result)
        stored_job = next(job for job in jobs_payload["jobs"] if job["job_id"] == job_id)
        assert stored_job["media"]["path"] == str(DEMO_MP4)

        # 8. Issues #13 + #14 on the wire: every served frame carries an
        # absolute existing path AND its validity span.
        for frame in moment_meta["frames"]:
            assert Path(frame["path"]).is_absolute()
            assert Path(frame["path"]).is_file(), frame["path"]
            assert frame["valid_from_ms"] <= frame["t_ms"] < frame["valid_to_ms"], frame
        extract_result = await session.call_tool(
            "extract_frame", {"job_id": job_id, "at_ms": 6500}
        )
        assert isinstance(extract_result, types.CallToolResult)
        assert not extract_result.is_error
        extract_text = next(
            block for block in extract_result.content if isinstance(block, types.TextContent)
        )
        extract_meta = json.loads(extract_text.text)
        assert Path(extract_meta["path"]).is_absolute()
        assert Path(extract_meta["path"]).is_file(), extract_meta["path"]

        # 9. Diarization over the wire — or its actionable error without the extra.
        if diarize.engine_available():
            diarized_result = await session.call_tool(
                "process_media",
                {
                    "path": str(TWO_VOICE_M4A),
                    "diarize": True,
                    "num_speakers": TWO_VOICE_NUM_SPEAKERS,
                },
                read_timeout_seconds=PROCESS_TIMEOUT,
            )
            diarized_summary = _payload(diarized_result)
            diarized_job_id = diarized_summary["job_id"]
            block = diarized_summary["diarization"]
            assert block["available"] is True
            assert block["detected_num_speakers"] == TWO_VOICE_NUM_SPEAKERS
            assert block["attribution_precision"] == "word"
            assert [speaker["label"] for speaker in block["speakers"]] == ["S1", "S2"]
            # v0.3.0: the roster names both the screen-check anchor and duration;
            # the old ambiguous field remains an additive compatibility alias.
            assert all(
                isinstance(speaker["longest_turn_at_ms"], int)
                and isinstance(speaker["longest_turn_duration_ms"], int)
                and speaker["longest_turn_ms"] == speaker["longest_turn_at_ms"]
                for speaker in block["speakers"]
            )
            assert any(
                segment.get("speaker")
                for segment in diarized_summary["transcript"]["preview_segments"]
            )
            srt_diarized = _payload(
                await session.call_tool(
                    "get_transcript",
                    {"job_id": diarized_summary["job_id"], "format": "srt"},
                )
            )
            assert "S1: " in srt_diarized["srt"]
            assert srt_diarized["media_kind"] == "audio"
            transcript_json = _payload(
                await session.call_tool(
                    "get_transcript", {"job_id": diarized_summary["job_id"]}
                )
            )
            assert transcript_json["attribution_precision"] == "word"
            assert "words" not in transcript_json, "raw word timings stay manifest-only"

            # v0.2.2: speaker= filter on the wire — one voice, case-normalized,
            # with the ocr-exclusion note in the payload.
            s1_hits = _payload(
                await session.call_tool(
                    "search",
                    {
                        "job_id": diarized_summary["job_id"],
                        "query": "the",
                        "speaker": "s1",
                    },
                )
            )
            assert s1_hits["speaker"] == "S1"
            assert s1_hits["hits"], "S1 speaks first — 'the' must hit her turns"
            assert all(hit["speaker"] == "S1" for hit in s1_hits["hits"])
            assert "ocr hits are excluded" in s1_hits["note"]

            # v0.2.3: a label outside the roster names the roster instead of
            # returning a mute empty list
            bogus_label = _payload(
                await session.call_tool(
                    "search",
                    {
                        "job_id": diarized_summary["job_id"],
                        "query": "the",
                        "speaker": "S99",
                    },
                )
            )
            assert bogus_label["hits"] == []
            assert "not in this job's roster (S1-S2)" in bogus_label["note"]

            # v0.3.0: persist names through the eighth tool. The actual reads
            # happen in a newly initialized session below.
            labelled = _payload(
                await session.call_tool(
                    "label_speakers",
                    {
                        "job_id": diarized_summary["job_id"],
                        "labels": {"S1": "Samantha", "S2": "Daniel"},
                        "evidence": {
                            "S1": "fixture voice order",
                            "S2": "fixture voice order",
                        },
                    },
                )
            )
            assert labelled["mapping_count"] == 2
            assert labelled["speakers"][0]["speaker_name"] == "Samantha"
        else:
            failed = await session.call_tool(
                "process_media",
                {"path": str(TWO_VOICE_M4A), "diarize": True},
                read_timeout_seconds=PROCESS_TIMEOUT,
            )
            assert isinstance(failed, types.CallToolResult)
            assert failed.is_error, "explicit diarize without the extra must error"
            error_text = failed.content[0]
            assert isinstance(error_text, types.TextContent)
            assert "[diarization]" in error_text.text

    if diarized_job_id is not None:
        # A second MCP process/session proves the mapping is durable rather
        # than cached in one Python object.
        async with (
            stdio_client(_server_params(home)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            transcript = _payload(
                await session.call_tool("get_transcript", {"job_id": diarized_job_id})
            )
            assert {entry["speaker_name"] for entry in transcript["speakers"]} == {
                "Samantha",
                "Daniel",
            }
            assert any(
                item.get("speaker") == "S1" and item.get("speaker_name") == "Samantha"
                for item in transcript["segments"]
            )
            named_srt = _payload(
                await session.call_tool(
                    "get_transcript", {"job_id": diarized_job_id, "format": "srt"}
                )
            )["srt"]
            assert "Samantha (S1): " in named_srt
            named_hits = _payload(
                await session.call_tool(
                    "search",
                    {"job_id": diarized_job_id, "query": "the", "speaker": "samantha"},
                )
            )
            assert named_hits["hits"]
            assert all(hit["speaker_name"] == "Samantha" for hit in named_hits["hits"])

            unsafe_force = await session.call_tool(
                "process_media",
                {
                    "path": str(TWO_VOICE_M4A),
                    "force": True,
                    "diarize": False,
                },
                read_timeout_seconds=PROCESS_TIMEOUT,
            )
            assert unsafe_force.is_error
            unsafe_error = unsafe_force.content[0]
            assert isinstance(unsafe_error, types.TextContent)
            assert "force=true, diarize=true" in unsafe_error.text
            survivor = _payload(
                await session.call_tool("get_transcript", {"job_id": diarized_job_id})
            )
            assert {entry["speaker_name"] for entry in survivor["speakers"]} == {
                "Samantha",
                "Daniel",
            }

            forced = _payload(
                await session.call_tool(
                    "process_media",
                    {
                        "path": str(TWO_VOICE_M4A),
                        "force": True,
                        "diarize": True,
                        "num_speakers": TWO_VOICE_NUM_SPEAKERS,
                    },
                    read_timeout_seconds=PROCESS_TIMEOUT,
                )
            )
            assert forced["reused"] is False
            assert "force_identity_review_note" in forced["diarization"]
            assert forced["diarization"]["speaker_names_pending_review"] == {
                "S1": "Samantha",
                "S2": "Daniel",
            }
            removed = _payload(
                await session.call_tool(
                    "label_speakers", {"job_id": diarized_job_id, "labels": {"S1": None}}
                )
            )
            assert removed["mapping_count"] == 0
            assert removed["speaker_names_pending_review"] == {"S2": "Daniel"}

        # The successful force and pending cleanup must survive another MCP
        # process rather than living only in the prior server's Python state.
        async with (
            stdio_client(_server_params(home)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            persisted = _payload(
                await session.call_tool("get_transcript", {"job_id": diarized_job_id})
            )
            assert "speaker_name" not in persisted["speakers"][0]
            assert persisted["speaker_names_pending_review"] == {"S2": "Daniel"}

    errlog.flush()
    errlog.seek(0)
    stderr = errlog.read()
    errlog.close()
    assert "MCPDeprecationWarning" not in stderr
    assert "logging capability is deprecated" not in stderr


@pytest.mark.timeout(900)
def test_mcp_stdio_end_to_end(tmp_path: Path) -> None:
    asyncio.run(_run_session(tmp_path / "talkthrough-home"))


async def _run_pending_review_session(home: Path) -> None:
    manifest = make_manifest()
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.segments = attribute_segments(manifest.transcript.segments, turns)
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=turns,
        speaker_names_pending_review={
            "S1": "Samantha",
            "S2": "Daniel",
            "S3": "Ghost",
        },
        speaker_name_evidence_pending_review={
            "S1": "old fixture order",
            "S2": "old fixture order",
            "S3": "old third cluster",
        },
        speaker_names_pending_review_context={
            "S1": PendingSpeakerReviewContext(longest_turn_at_ms=0),
            "S2": PendingSpeakerReviewContext(longest_turn_at_ms=5000),
            "S3": PendingSpeakerReviewContext(
                source_detected_num_speakers=3,
                longest_turn_at_ms=9000,
            ),
        },
        labels_changed=True,
    )
    save_manifest(manifest, home / "jobs" / manifest.job_id)

    async with (
        stdio_client(_server_params(home)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        transcript = _payload(
            await session.call_tool("get_transcript", {"job_id": manifest.job_id})
        )
        assert transcript["speaker_names_pending_review"] == {
            "S1": "Samantha",
            "S2": "Daniel",
            "S3": "Ghost",
        }
        assert transcript["speaker_names_pending_review_stale_labels"] == ["S3"]
        assert transcript["speaker_names_pending_review_context"]["S3"] == {
            "source_detected_num_speakers": 3,
            "longest_turn_at_ms": 9000,
        }
        assert "not active identities" in transcript["speaker_names_pending_review_note"]
        assert all("speaker_name" not in segment for segment in transcript["segments"])
        pending_search = _payload(
            await session.call_tool(
                "search",
                {"job_id": manifest.job_id, "query": "the", "speaker": "samantha"},
            )
        )
        assert pending_search["hits"] == []
        assert "saved in pending review" in pending_search["note"]
        jobs_payload = _payload(await session.call_tool("list_jobs", {}))
        entry = next(job for job in jobs_payload["jobs"] if job["job_id"] == manifest.job_id)
        assert entry["speaker_names_pending_review_count"] == 3
        removed = _payload(
            await session.call_tool(
                "label_speakers",
                {"job_id": manifest.job_id, "labels": {"S3": None}},
            )
        )
        assert removed["speaker_names_pending_review"] == {
            "S1": "Samantha",
            "S2": "Daniel",
        }
        assert "speaker_names_pending_review_stale_labels" not in removed

    # A new server process proves stale cleanup and the remaining context are
    # durable rather than cached in one SDK session.
    async with (
        stdio_client(_server_params(home)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        transcript = _payload(
            await session.call_tool("get_transcript", {"job_id": manifest.job_id})
        )
        assert transcript["speaker_names_pending_review"] == {
            "S1": "Samantha",
            "S2": "Daniel",
        }
        assert set(transcript["speaker_names_pending_review_context"]) == {"S1", "S2"}
        assert "speaker_names_pending_review_stale_labels" not in transcript


@pytest.mark.timeout(900)
def test_fresh_mcp_session_serves_pending_review_without_activating_names(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_pending_review_session(tmp_path / "pending-review-home"))
