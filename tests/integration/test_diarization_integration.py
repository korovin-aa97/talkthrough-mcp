"""Diarization integration on the committed two-voice fixture (real engine).

Runs only when the [diarization] extra is installed. Models are downloaded
once into a stable per-machine cache (`~/.cache/talkthrough-test-models`,
overridable via TALKTHROUGH_TEST_MODEL_CACHE — CI persists it with
actions/cache) and handed to the pipeline through the offline-preseed env
paths, so the per-test TALKTHROUGH_HOME stays a throwaway tmp dir.

CI determinism: every engine run here passes an explicit ``num_speakers`` —
threshold-mode clustering on synthetic TTS voices is the documented flaky
path. Threshold-mode behavior is exercised by the manual hostile pass.

Ordering note: tests that MUTATE the job store come last.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.integration.fixture_facts import (
    MEETING_M4A,
    TWO_VOICE_KEYWORDS,
    TWO_VOICE_M4A,
    TWO_VOICE_NUM_SPEAKERS,
    TWO_VOICE_TURNS_MS,
)

from talkthrough_mcp.core import diarize, jobs, pipeline
from talkthrough_mcp.core.pipeline import ProcessResult

pytestmark = [
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not diarize.engine_available(), reason="sherpa-onnx [diarization] extra not installed"
    ),
]

_ENV_KEYS = (
    "TALKTHROUGH_HOME",
    "TALKTHROUGH_WHISPER_MODEL",
    "TALKTHROUGH_DIARIZE",
    "TALKTHROUGH_DIARIZATION_SEG_MODEL",
    "TALKTHROUGH_DIARIZATION_EMB_MODEL",
    "TALKTHROUGH_DIARIZATION_THRESHOLD",
)


def _models_cache() -> Path:
    override = os.environ.get("TALKTHROUGH_TEST_MODEL_CACHE")
    return Path(override) if override else Path.home() / ".cache" / "talkthrough-test-models"


@pytest.fixture(scope="session")
def diarization_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}

    # Download-once into the stable cache (exercises ensure_model_file for
    # real on a cold machine), then preseed the tmp-home run via env paths.
    os.environ["TALKTHROUGH_HOME"] = str(_models_cache())
    seg_path = diarize.ensure_model_file(
        diarize.SEGMENTATION_MODELS[diarize.DEFAULT_SEGMENTATION_MODEL]
    )
    emb_path = diarize.ensure_model_file(
        diarize.EMBEDDING_MODELS[diarize.DEFAULT_EMBEDDING_MODEL]
    )

    home = tmp_path_factory.mktemp("talkthrough-diarize-home")
    os.environ["TALKTHROUGH_HOME"] = str(home)
    os.environ["TALKTHROUGH_WHISPER_MODEL"] = "tiny"
    os.environ["TALKTHROUGH_DIARIZATION_SEG_MODEL"] = str(seg_path)
    os.environ["TALKTHROUGH_DIARIZATION_EMB_MODEL"] = str(emb_path)
    os.environ.pop("TALKTHROUGH_DIARIZE", None)
    os.environ.pop("TALKTHROUGH_DIARIZATION_THRESHOLD", None)
    yield home
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session")
def two_voice(diarization_home: Path) -> ProcessResult:
    return pipeline.process_media(
        str(TWO_VOICE_M4A),
        diarize_speakers=True,
        num_speakers=TWO_VOICE_NUM_SPEAKERS,
    )


def _expected_label(t0_ms: int, t1_ms: int) -> str | None:
    midpoint = (t0_ms + t1_ms) / 2
    for start, end, label in TWO_VOICE_TURNS_MS:
        if start <= midpoint <= end:
            return label
    return None


# --- fresh run ----------------------------------------------------------------


def test_exactly_two_speakers_detected(two_voice: ProcessResult) -> None:
    diarization = two_voice.manifest.transcript.diarization
    assert diarization is not None and diarization.available
    assert diarization.detected_num_speakers == TWO_VOICE_NUM_SPEAKERS
    assert diarization.requested_num_speakers == TWO_VOICE_NUM_SPEAKERS
    assert [stat.label for stat in diarization.speakers] == ["S1", "S2"]
    assert diarization.engine == "sherpa-onnx"
    assert diarization.turns, "turns must be persisted for range queries and amends"


def test_segments_attributed_per_fixture_facts(two_voice: ProcessResult) -> None:
    segments = two_voice.manifest.transcript.segments
    assert segments, "whisper tiny must produce segments"
    scored = [
        (segment.speaker, _expected_label(segment.t0_ms, segment.t1_ms))
        for segment in segments
    ]
    comparable = [(got, want) for got, want in scored if want is not None]
    matches = sum(1 for got, want in comparable if got == want)
    assert matches / len(comparable) >= 0.8, f"attribution disagrees with facts: {scored}"


def test_two_voice_keywords_survive_whisper(two_voice: ProcessResult) -> None:
    text = two_voice.manifest.transcript.full_text().lower()
    assert any(keyword in text for keyword in TWO_VOICE_KEYWORDS), text


def test_roster_aggregates_are_consistent(two_voice: ProcessResult) -> None:
    diarization = two_voice.manifest.transcript.diarization
    assert diarization is not None
    for stat in diarization.speakers:
        turns = [turn for turn in diarization.turns if turn.speaker == stat.label]
        assert stat.turn_count == len(turns)
        assert stat.talk_time_ms == sum(turn.t1_ms - turn.t0_ms for turn in turns)
        assert stat.first_ms == min(turn.t0_ms for turn in turns)
        assert stat.last_ms == max(turn.t1_ms for turn in turns)


def test_diarization_survives_disk_round_trip(two_voice: ProcessResult) -> None:
    loaded = jobs.load_job(two_voice.manifest.job_id)
    assert loaded == two_voice.manifest


def test_summary_carries_compact_diarization_block(two_voice: ProcessResult) -> None:
    summary = pipeline.summarize(two_voice)
    block = summary["diarization"]
    assert block["available"] is True
    assert block["detected_num_speakers"] == TWO_VOICE_NUM_SPEAKERS
    assert block["requested_num_speakers"] == TWO_VOICE_NUM_SPEAKERS
    assert {"label", "talk_time_ms", "turn_count"} == set(block["speakers"][0])
    preview = summary["transcript"]["preview_segments"]
    assert any(entry.get("speaker") for entry in preview)


def test_idempotent_rerun_reuses_without_amend(two_voice: ProcessResult) -> None:
    rerun = pipeline.process_media(
        str(TWO_VOICE_M4A), diarize_speakers=True, num_speakers=TWO_VOICE_NUM_SPEAKERS
    )
    assert rerun.reused is True
    assert rerun.amended is False
    assert rerun.elapsed_s < 5
    assert rerun.manifest == two_voice.manifest


def test_non_diarize_call_keeps_the_superset(two_voice: ProcessResult) -> None:
    rerun = pipeline.process_media(str(TWO_VOICE_M4A))
    assert rerun.reused is True
    assert rerun.amended is False
    diarization = rerun.manifest.transcript.diarization
    assert diarization is not None and diarization.available


# --- amend path (mutates its own job — meeting fixture, single voice) ----------


def test_amend_adds_diarization_without_rerunning_whisper(
    diarization_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = pipeline.process_media(str(MEETING_M4A))
    assert plain.manifest.transcript.diarization is None
    baseline_texts = [segment.text for segment in plain.manifest.transcript.segments]

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("whisper must not re-run during a diarization amend")

    monkeypatch.setattr(pipeline.stt, "transcribe", boom)
    amended = pipeline.process_media(str(MEETING_M4A), diarize_speakers=True, num_speakers=1)
    assert amended.reused is True
    assert amended.amended is True
    assert amended.manifest.created_at == plain.manifest.created_at
    assert [s.text for s in amended.manifest.transcript.segments] == baseline_texts

    diarization = amended.manifest.transcript.diarization
    assert diarization is not None and diarization.available
    assert diarization.detected_num_speakers == 1
    assert diarization.requested_num_speakers == 1
    assert all(segment.speaker == "S1" for segment in amended.manifest.transcript.segments)

    # persisted, not just in-memory
    assert jobs.load_job(amended.manifest.job_id) == amended.manifest


def test_amend_again_on_explicit_num_speakers_mismatch(
    diarization_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("whisper must not re-run during a diarization amend")

    monkeypatch.setattr(pipeline.stt, "transcribe", boom)
    reclustered = pipeline.process_media(str(MEETING_M4A), diarize_speakers=True, num_speakers=2)
    assert reclustered.amended is True
    diarization = reclustered.manifest.transcript.diarization
    assert diarization is not None and diarization.requested_num_speakers == 2

    settled = pipeline.process_media(str(MEETING_M4A), diarize_speakers=True, num_speakers=2)
    assert settled.reused is True
    assert settled.amended is False
