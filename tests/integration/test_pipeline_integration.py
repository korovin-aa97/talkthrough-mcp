"""Full-pipeline integration on the committed fixtures (real ffmpeg + whisper tiny + OCR).

Ordering note: tests that MUTATE the job store (force reprocess, caps with
force) are placed last — everything above them reads the session-scoped
first processing result.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.integration.fixture_facts import (
    CREATION_TIME_ISO,
    DEMO_MP4,
    MEETING_KEYWORDS,
    MEETING_M4A,
    OCR_SCENE_WORD,
    SCENE_BOUNDARIES_MS,
    SCENE_TOLERANCE_MS,
    SCRIPT_KEYWORDS,
)

from talkthrough_mcp.core import jobs, pipeline
from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.manifest import search_manifest
from talkthrough_mcp.core.pipeline import ProcessResult

pytestmark = pytest.mark.timeout(900)

_ENV_KEYS = ("TALKTHROUGH_HOME", "TALKTHROUGH_WHISPER_MODEL")


@pytest.fixture(scope="session")
def integration_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    home = tmp_path_factory.mktemp("talkthrough-home")
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ["TALKTHROUGH_HOME"] = str(home)
    os.environ["TALKTHROUGH_WHISPER_MODEL"] = "tiny"
    yield home
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session")
def demo(integration_home: Path) -> ProcessResult:
    return pipeline.process_media(str(DEMO_MP4))


@pytest.fixture(scope="session")
def meeting(integration_home: Path) -> ProcessResult:
    return pipeline.process_media(str(MEETING_M4A))


# --- video fixture: transcript, frames, wall clock, ocr, search -------------


def test_transcript_contains_script_keywords(demo: ProcessResult) -> None:
    text = demo.manifest.transcript.full_text().lower()
    found = [keyword for keyword in SCRIPT_KEYWORDS if keyword in text]
    assert len(found) >= 2, f"whisper tiny lost too many keywords; got {text!r}"
    assert demo.manifest.transcript.language == "en"


def test_unique_frames_cover_scene_boundaries(demo: ProcessResult) -> None:
    unique_ms = [frame.ms for frame in demo.manifest.unique_frames()]
    assert len(unique_ms) >= 3
    for boundary in SCENE_BOUNDARIES_MS:
        nearest = min(abs(ms - boundary) for ms in unique_ms)
        assert nearest <= SCENE_TOLERANCE_MS, (
            f"no unique frame within {SCENE_TOLERANCE_MS}ms of scene boundary {boundary}ms "
            f"(unique frames at {unique_ms})"
        )


def test_wall_clock_comes_from_container_metadata(demo: ProcessResult) -> None:
    clock = demo.manifest.wall_clock
    assert clock is not None
    assert clock.source == "metadata"
    assert clock.confidence == "medium"
    assert clock.start_utc.isoformat(timespec="seconds") == CREATION_TIME_ISO
    assert demo.manifest.t_wall_iso(6000) == "2026-07-10T10:00:06+00:00"


def test_ocr_reads_scene_titles(demo: ProcessResult) -> None:
    texts = [frame.ocr_text or "" for frame in demo.manifest.unique_frames()]
    assert any(OCR_SCENE_WORD.lower() in text.lower() for text in texts), (
        f"no unique frame OCR contains {OCR_SCENE_WORD!r}: {texts}"
    )


def test_search_hits_transcript_and_ocr(demo: ProcessResult) -> None:
    hits = search_manifest(demo.manifest, "dashboard")
    sources = {hit.source for hit in hits}
    assert "transcript" in sources, "spoken 'dashboard' not found"
    assert "ocr" in sources, "on-screen 'DASHBOARD' not found via OCR"
    assert all(hit.t_wall is not None for hit in hits)


def test_frames_scaled_within_vision_budget(demo: ProcessResult) -> None:
    from PIL import Image

    directory = jobs.frames_dir(demo.manifest.job_id)
    for frame in demo.manifest.unique_frames():
        with Image.open(directory / frame.file) as image:
            assert image.width <= 1568


def test_idempotent_rerun_is_instant(demo: ProcessResult) -> None:
    rerun = pipeline.process_media(str(DEMO_MP4))
    assert rerun.reused is True
    assert rerun.elapsed_s < 5
    assert rerun.manifest == demo.manifest


# --- server tool layer on the processed job ---------------------------------


def test_get_transcript_srt_is_valid(demo: ProcessResult) -> None:
    from talkthrough_mcp.server import get_transcript

    payload = get_transcript(demo.manifest.job_id, format="srt")
    srt = payload["srt"]
    assert srt.startswith("1\n00:00:0")
    assert " --> " in srt
    assert payload["truncated"] is False


def test_get_moment_bundles_slice_frames_ocr(demo: ProcessResult) -> None:
    from mcp.server.fastmcp import Image as McpImage

    from talkthrough_mcp.server import get_moment

    content = get_moment(demo.manifest.job_id, 5000, 9000)
    meta = json.loads(content[0])
    images = [block for block in content[1:] if isinstance(block, McpImage)]
    assert meta["transcript"], "moment must include the transcript slice"
    assert meta["frames"], "moment must include frame refs"
    assert 1 <= len(images) <= 3
    assert meta["range"]["t_wall_start"] == "2026-07-10T10:00:05+00:00"


def test_extract_frame_full_resolution_and_crop(demo: ProcessResult, tmp_path: Path) -> None:
    from PIL import Image

    from talkthrough_mcp.server import extract_frame

    content = extract_frame(demo.manifest.job_id, at_ms=6500)
    extract_path = jobs.job_dir(demo.manifest.job_id) / "extracts" / "extract-t00006500.jpg"
    assert extract_path.is_file()
    with Image.open(extract_path) as image:
        assert (image.width, image.height) == (1280, 720)  # native, not keyframe-scaled
    assert len(content) == 2

    extract_frame(demo.manifest.job_id, at_ms=6500, crop={"x": 0, "y": 0, "w": 200, "h": 100})
    extracts_dir = jobs.job_dir(demo.manifest.job_id) / "extracts"
    with Image.open(extracts_dir / "extract-t00006500-crop0x0x200x100.jpg") as image:
        assert (image.width, image.height) == (200, 100)


# --- audio-only fixture ------------------------------------------------------


def test_audio_only_yields_transcript_only_manifest(meeting: ProcessResult) -> None:
    manifest = meeting.manifest
    assert manifest.media.kind == "audio"
    assert manifest.media.has_video is False
    assert manifest.transcript.available is True
    assert manifest.frames.count == 0
    text = manifest.transcript.full_text().lower()
    assert any(keyword in text for keyword in MEETING_KEYWORDS), text


def test_audio_only_frame_tools_error_clearly(meeting: ProcessResult) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from talkthrough_mcp.server import extract_frame, get_frames, get_moment

    with pytest.raises(ToolError, match="audio-only"):
        get_frames(meeting.manifest.job_id, at_ms=1000)
    with pytest.raises(ToolError, match="audio-only"):
        extract_frame(meeting.manifest.job_id, at_ms=1000)

    content = get_moment(meeting.manifest.job_id, 0, 4000)
    meta = json.loads(content[0])
    assert "audio-only" in meta["note"]
    assert meta["frames"] == []
    assert len(content) == 1  # no image blocks


# --- multilingual: language detection is plumbed through ----------------------


def test_russian_narration_detected_and_reported(integration_home: Path) -> None:
    from tests.integration.fixture_facts import RU_LANGUAGE, RU_M4A

    result = pipeline.process_media(str(RU_M4A))
    transcript = result.manifest.transcript
    assert transcript.language == RU_LANGUAGE
    assert (transcript.language_probability or 0) > 0.5
    assert transcript.segments, "russian narration must produce segments even on tiny"
    summary = pipeline.summarize(result)
    assert summary["transcript"]["language"] == RU_LANGUAGE
    assert summary["transcript"]["language_probability"] == transcript.language_probability


# --- mutating tests: keep these LAST -----------------------------------------


def test_caps_are_enforced(
    demo: ProcessResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALKTHROUGH_MAX_SECONDS", "5")
    with pytest.raises(ValidationError, match="exceeds the 5s cap"):
        pipeline.process_media(str(DEMO_MP4), force=True)


def test_force_reprocess_with_recorded_at_re_anchors(demo: ProcessResult) -> None:
    result = pipeline.process_media(
        str(DEMO_MP4), recorded_at="2026-07-10T12:00:00+02:00", force=True
    )
    assert result.reused is False
    clock = result.manifest.wall_clock
    assert clock is not None
    assert clock.source == "override"
    assert clock.confidence == "exact"
    assert clock.start_utc.isoformat(timespec="seconds") == CREATION_TIME_ISO
    assert clock.tz_offset_min == 120
    assert result.manifest.t_wall_iso(0) == "2026-07-10T12:00:00+02:00"


def test_explicit_model_mismatch_triggers_reprocess(
    demo: ProcessResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit different `model=` must NOT silently return the old-model
    manifest (launch-day E2E catch); same explicit model still reuses instantly.
    Transcription is stubbed so no extra whisper model downloads in CI."""
    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("transcribe")
        raise RuntimeError("reprocess attempted")

    monkeypatch.setattr(pipeline.stt, "transcribe", boom)
    rerun = pipeline.process_media(str(DEMO_MP4), model="tiny")
    assert rerun.reused is True
    assert not calls, "matching explicit model must not reprocess"
    with pytest.raises(RuntimeError, match="reprocess attempted"):
        pipeline.process_media(str(DEMO_MP4), model="base")
    assert calls == ["transcribe"]
