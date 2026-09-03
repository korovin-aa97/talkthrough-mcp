"""Whisper model resolution + diarize request matrix + amend gating."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import diarize
from talkthrough_mcp.core.diarize import Diarization, PendingSpeakerReviewContext
from talkthrough_mcp.core.errors import ToolFailureError, ValidationError
from talkthrough_mcp.core.pipeline import (
    ALLOWED_WHISPER_MODELS,
    DEFAULT_WHISPER_MODEL,
    _needs_diarize_amend,
    _resolve_diarize_request,
    resolve_whisper_model,
)


def test_default_comes_from_env_or_small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_WHISPER_MODEL", raising=False)
    assert resolve_whisper_model(None) == DEFAULT_WHISPER_MODEL
    monkeypatch.setenv("TALKTHROUGH_WHISPER_MODEL", "large-v3-turbo")
    assert resolve_whisper_model(None) == "large-v3-turbo"


def test_per_call_override_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_WHISPER_MODEL", "small")
    assert resolve_whisper_model("medium") == "medium"


def test_unknown_model_fails_fast_with_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="large-v3-turbo"):
        resolve_whisper_model("gpt-whisper-9000")
    monkeypatch.setenv("TALKTHROUGH_WHISPER_MODEL", "bogus-env-model")
    with pytest.raises(ValidationError, match="bogus-env-model"):
        resolve_whisper_model(None)


def test_allowlist_covers_the_documented_tiers() -> None:
    assert {"tiny", "small", "medium", "large-v3", "large-v3-turbo", "turbo"} <= (
        ALLOWED_WHISPER_MODELS
    )


# --- diarize request matrix ---------------------------------------------------


def engine(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> None:
    monkeypatch.setattr(diarize, "engine_available", lambda: available)


def test_diarize_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    request = _resolve_diarize_request(None, None)
    assert request.run is False
    assert request.explicit is False
    assert request.engine_missing is False


def test_explicit_true_without_extra_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    engine(monkeypatch, available=False)
    with pytest.raises(ValidationError, match=r"\[diarization\]"):
        _resolve_diarize_request(True, None)


def test_env_default_without_extra_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "on")
    engine(monkeypatch, available=False)
    request = _resolve_diarize_request(None, None)
    assert request.run is False
    assert request.engine_missing is True


def test_env_default_with_extra_runs_non_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "on")
    engine(monkeypatch, available=True)
    request = _resolve_diarize_request(None, None)
    assert request.run is True
    assert request.explicit is False


def test_explicit_false_beats_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "on")
    engine(monkeypatch, available=True)
    assert _resolve_diarize_request(False, None).run is False


def test_num_speakers_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    engine(monkeypatch, available=True)
    with pytest.raises(ValidationError, match=">= 1"):
        _resolve_diarize_request(True, 0)
    with pytest.raises(ValidationError, match="diarize=true"):
        _resolve_diarize_request(False, 2)
    request = _resolve_diarize_request(True, 2)
    assert request.run is True and request.num_speakers == 2


def test_num_speakers_alone_is_explicit_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    engine(monkeypatch, available=True)
    request = _resolve_diarize_request(None, 3)
    assert request.run is True
    assert request.explicit is True
    engine(monkeypatch, available=False)
    with pytest.raises(ValidationError, match=r"\[diarization\]"):
        _resolve_diarize_request(None, 3)


# --- amend gating ---------------------------------------------------------------


def request_for(
    monkeypatch: pytest.MonkeyPatch, diarize_flag: bool | None, num_speakers: int | None
):
    engine(monkeypatch, available=True)
    return _resolve_diarize_request(diarize_flag, num_speakers)


def diarized(requested: int | None = None) -> Diarization:
    return Diarization(available=True, reason="", requested_num_speakers=requested)


def test_explicit_diarize_on_plain_job_amends(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = make_manifest()
    assert manifest.transcript.diarization is None
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is True


def test_ambient_env_on_never_amends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "on")
    manifest = make_manifest()
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, None, None)) is False


def test_diarized_job_with_same_or_no_k_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = make_manifest()
    manifest.transcript.diarization = diarized(requested=2)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, 2)) is False


def test_explicit_k_mismatch_amends(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = make_manifest()
    manifest.transcript.diarization = diarized(requested=None)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, 3)) is True
    manifest.transcript.diarization = diarized(requested=2)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, 4)) is True


def test_previously_failed_diarization_amends_on_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_manifest()
    manifest.transcript.diarization = Diarization(available=False, reason="engine exploded")
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is True


def test_no_audio_job_never_amends(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = make_manifest()
    manifest.media = type(manifest.media)(
        **{**manifest.media.__dict__, "has_audio": False}
    )
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False


# --- re-diarize on embedding-model change (v0.2.2) ----------------------------


def emb_diarized(embedding_model: str | None) -> Diarization:
    return Diarization(available=True, reason="", embedding_model=embedding_model)


def test_explicit_diarize_amends_when_emb_model_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", "wespeaker_en_voxceleb_resnet34_LM"
    )
    manifest = make_manifest()
    manifest.transcript.diarization = emb_diarized(diarize.DEFAULT_EMBEDDING_MODEL)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is True


def test_matching_emb_model_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = make_manifest()
    manifest.transcript.diarization = emb_diarized(diarize.DEFAULT_EMBEDDING_MODEL)
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", raising=False)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", diarize.DEFAULT_EMBEDDING_MODEL)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False


def test_emb_env_change_without_explicit_diarize_never_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the whisper-model rule: only explicit intent re-runs."""
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "on")
    monkeypatch.setenv(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", "wespeaker_en_voxceleb_resnet34_LM"
    )
    manifest = make_manifest()
    manifest.transcript.diarization = emb_diarized(diarize.DEFAULT_EMBEDDING_MODEL)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, None, None)) is False


def test_local_onnx_path_env_counts_as_a_model_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    onnx = tmp_path / "custom.onnx"
    onnx.write_bytes(b"onnx")
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", str(onnx))
    manifest = make_manifest()
    manifest.transcript.diarization = emb_diarized(diarize.DEFAULT_EMBEDDING_MODEL)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is True
    # a job stored FROM that path matches it on the next explicit call
    manifest.transcript.diarization = emb_diarized(str(onnx))
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False


def test_manifest_without_emb_label_skips_the_emb_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", "wespeaker_en_voxceleb_resnet34_LM"
    )
    manifest = make_manifest()
    manifest.transcript.diarization = emb_diarized(None)
    assert _needs_diarize_amend(manifest, request_for(monkeypatch, True, None)) is False


def test_resolved_embedding_label_never_touches_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diarize, "ensure_model_file", lambda spec: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", raising=False)
    assert diarize.resolved_embedding_label() == diarize.DEFAULT_EMBEDDING_MODEL
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", "nemo_en_titanet_small")
    assert diarize.resolved_embedding_label() == "nemo_en_titanet_small"
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", "~/models/x.onnx")
    label = diarize.resolved_embedding_label()
    assert label == str(Path("~/models/x.onnx").expanduser())  # platform-native
    assert "~" not in label


# --- diarization_amended reflects the OUTCOME (v0.2.2 honesty fix) -------------


def _stored_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A real store entry whose job_id matches a real (tiny) media file."""
    from talkthrough_mcp.core import jobs
    from talkthrough_mcp.core.manifest import save_manifest

    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path / "home"))
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not really a video, only hashed")
    job_id = jobs.compute_job_id(media)
    manifest = make_manifest(job_id=job_id)
    manifest.media = type(manifest.media)(
        **{**manifest.media.__dict__, "path": str(media)}
    )
    directory = jobs.job_dir(job_id)
    directory.mkdir(parents=True)
    save_manifest(manifest, directory)
    return media


def _run_amend(media, monkeypatch: pytest.MonkeyPatch, *, succeed: bool):
    from talkthrough_mcp.core import audio, pipeline
    from talkthrough_mcp.core.diarize import Diarization

    engine(monkeypatch, available=True)
    # the amend path constructs the engine BEFORE touching anything (v0.2.3
    # fail-fast) — stub it so unit runs never resolve real models
    monkeypatch.setattr(diarize, "create_diarizer", lambda: object())
    monkeypatch.setattr(audio, "extract_wav", lambda *a, **k: None)

    def fake_run(wav_path, transcript, request, report, diarizer=None) -> None:
        transcript.diarization = (
            Diarization(available=True, reason="", detected_num_speakers=1)
            if succeed
            else Diarization(available=False, reason="model download failed: TLS")
        )

    monkeypatch.setattr(pipeline, "_run_diarization", fake_run)
    return pipeline.process_media(str(media), diarize_speakers=True)


def test_failed_amend_does_not_claim_diarization_amended(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs

    media = _stored_job(tmp_path, monkeypatch)
    manifest_path = jobs.job_dir(jobs.compute_job_id(media)) / "manifest.json"
    before = manifest_path.read_bytes()
    with pytest.raises(ToolFailureError, match=r"stored transcript.*kept unchanged.*TLS"):
        _run_amend(media, monkeypatch, succeed=False)
    assert manifest_path.read_bytes() == before


def test_successful_amend_still_reports_the_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import pipeline

    media = _stored_job(tmp_path, monkeypatch)
    result = _run_amend(media, monkeypatch, succeed=True)
    assert result.amended is True
    assert pipeline.summarize(result)["diarization_amended"] is True


# --- fail-fast failed amend (v0.2.3): construction errors leave the store alone


def _stored_diarized_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A stored job with GOOD labels whose next explicit call must amend
    (stored requested k=2, the test calls with k=3)."""
    from talkthrough_mcp.core import jobs
    from talkthrough_mcp.core.diarize import SpeakerStat, Turn
    from talkthrough_mcp.core.manifest import save_manifest

    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path / "home"))
    media = tmp_path / "meeting.mp4"
    media.write_bytes(b"two voices, only ever hashed")
    job_id = jobs.compute_job_id(media)
    manifest = make_manifest(job_id=job_id)
    manifest.media = type(manifest.media)(
        **{**manifest.media.__dict__, "path": str(media)}
    )
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        requested_num_speakers=2,
        detected_num_speakers=2,
        speakers=[
            SpeakerStat(label="S1", talk_time_ms=5000, turn_count=1, first_ms=0, last_ms=5000),
            SpeakerStat(label="S2", talk_time_ms=3000, turn_count=1, first_ms=5000, last_ms=8000),
        ],
        turns=turns,
    )
    directory = jobs.job_dir(job_id)
    directory.mkdir(parents=True)
    save_manifest(manifest, directory)
    return media, directory / "manifest.json"


@pytest.mark.parametrize(
    "exc",
    [
        ValidationError(
            "TALKTHROUGH_DIARIZATION_EMB_MODEL='nemo_titanet_smal' is neither a known "
            "model name nor an existing .onnx file"
        ),
        ToolFailureError("sherpa-onnx rejected the diarization model config"),
    ],
)
def test_amend_construction_failure_fails_fast_and_leaves_the_store_alone(
    tmp_path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """The 0.2.2-eval caveat: a failed explicit re-diarize must not clobber
    good stored labels. An engine that cannot even CONSTRUCT (mistyped model
    env, dead download) now raises before the WAV extract and before any
    manifest write — the store stays byte-identical."""
    from talkthrough_mcp.core import audio, jobs, pipeline

    media, manifest_path = _stored_diarized_job(tmp_path, monkeypatch)
    before = manifest_path.read_bytes()
    engine(monkeypatch, available=True)

    def refuse_construction():
        raise exc

    monkeypatch.setattr(diarize, "create_diarizer", refuse_construction)

    def no_extract(*args: object, **kwargs: object) -> None:
        raise AssertionError("fail-fast must fire BEFORE the WAV extract")

    monkeypatch.setattr(audio, "extract_wav", no_extract)
    with pytest.raises((ValidationError, ToolFailureError)) as caught:
        pipeline.process_media(str(media), diarize_speakers=True, num_speakers=3)
    assert str(caught.value) == str(exc)
    assert manifest_path.read_bytes() == before, "manifest must stay byte-identical"
    stored = jobs.load_job(jobs.compute_job_id(media)).transcript.diarization
    assert stored is not None and stored.available
    assert [stat.label for stat in stored.speakers] == ["S1", "S2"]


def test_fresh_run_construction_failure_still_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-fast is amend-only: a fresh pipeline has no labels to lose,
    so a construction failure keeps degrading to available:false + reason."""
    from talkthrough_mcp.core.manifest import Transcript
    from talkthrough_mcp.core.pipeline import _run_diarization

    def refuse_construction():
        raise ToolFailureError("could not download diarization model: network down")

    monkeypatch.setattr(diarize, "create_diarizer", refuse_construction)
    transcript = Transcript(
        available=True, reason="", language="en", model="tiny", segments=[]
    )
    _run_diarization(
        Path("/nonexistent.wav"),
        transcript,
        request_for(monkeypatch, True, None),
        lambda stage, fraction: None,
    )
    assert transcript.diarization is not None
    assert transcript.diarization.available is False
    assert "network down" in transcript.diarization.reason


def test_whisper_loads_from_local_cache_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm loads must be zero-network: try local_files_only=True, download on miss.

    Without this, huggingface_hub revalidates repo metadata against
    huggingface.co on EVERY cached model load (caught by the pre-HN
    socket-block test)."""
    import faster_whisper

    from talkthrough_mcp.core import stt

    calls: list[bool | None] = []
    cache_miss = [False]

    class Recorder:
        def __init__(self, name: str, **kwargs: object) -> None:
            calls.append(kwargs.get("local_files_only"))  # type: ignore[arg-type]
            if kwargs.get("local_files_only") and cache_miss[0]:
                raise RuntimeError("not in local cache")

    monkeypatch.setattr(faster_whisper, "WhisperModel", Recorder)

    stt._load_model("small")
    assert calls == [True], "cached path must never pass local_files_only=False"

    calls.clear()
    cache_miss[0] = True
    stt._load_model("small")
    assert calls == [True, None], "cache miss must fall back to a one-time download"


# --- pre-STT failure cleanup (v0.2.4) ------------------------------------------


def test_pre_stt_failure_cleans_up_the_partial_dir_and_retry_completes(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before the manifest exists (the reported case: cold-start
    model download) must remove the lock-only job dir; a later live retry
    starts clean and completes the same job_id."""
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.probe import MediaInfo

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake-video-bytes" * 1000)
    job_id = jobs.compute_job_id(media)

    def boom(path: Path) -> MediaInfo:
        raise ToolFailureError("simulated cold-start failure before STT")

    monkeypatch.setattr(pipeline, "probe_media", boom)
    with pytest.raises(ToolFailureError):
        pipeline.process_media(str(media))
    assert not jobs.job_dir(job_id).exists(), "partial dir (job.lock only) must be removed"
    assert jobs.list_jobs() == []

    # retry: a stream-less probe result walks the whole pipeline to a manifest
    # without needing ffmpeg/whisper — the directory is recreated and completed
    def probe_ok(path: Path) -> MediaInfo:
        stat = media.stat()
        return MediaInfo(
            path=str(media),
            filename=media.name,
            size_bytes=stat.st_size,
            duration_s=5.0,
            has_video=False,
            has_audio=False,
            width=0,
            height=0,
            video_codec="",
            mtime_epoch=stat.st_mtime,
        )

    monkeypatch.setattr(pipeline, "probe_media", probe_ok)
    result = pipeline.process_media(str(media))
    assert result.reused is False
    assert result.manifest.job_id == job_id
    assert jobs.job_exists(job_id)
    assert [m.job_id for m in jobs.list_jobs()] == [job_id]


# --- dusty-roster budget (threshold-mode honesty) ------------------------------


def dusty_diarization(majors: int, dust: int) -> Diarization:
    from talkthrough_mcp.core.diarize import SpeakerStat

    speakers = [
        SpeakerStat(label=f"S{i+1}", talk_time_ms=60_000 + i, turn_count=5,
                    first_ms=0, last_ms=1000)
        for i in range(majors)
    ] + [
        SpeakerStat(label=f"S{majors+i+1}", talk_time_ms=2_000, turn_count=1,
                    first_ms=0, last_ms=1000)
        for i in range(dust)
    ]
    return Diarization(
        available=True, reason="", detected_num_speakers=majors + dust,
        speakers=speakers,
    )


def test_roster_payload_caps_and_counts_hidden() -> None:
    from talkthrough_mcp.core.pipeline import SUMMARY_ROSTER_CAP, roster_payload

    entries, hidden = roster_payload(dusty_diarization(majors=5, dust=118))
    assert len(entries) == SUMMARY_ROSTER_CAP
    assert hidden == 5 + 118 - SUMMARY_ROSTER_CAP
    # top-by-talk-time, but label order preserved in the output
    assert [e["label"] for e in entries][:5] == ["S1", "S2", "S3", "S4", "S5"]

    small_entries, small_hidden = roster_payload(dusty_diarization(majors=3, dust=0))
    assert len(small_entries) == 3 and small_hidden == 0


def test_summary_threshold_mode_escalates_to_the_user() -> None:
    """v0.2.2: over-detection no longer claims a 'likely headcount' (an
    external eval falsified that: said 4, truth 2) — the note instructs the
    agent to ASK THE USER and names the num_speakers amend honestly."""
    from talkthrough_mcp.core.pipeline import _summarize_diarization

    block = _summarize_diarization(dusty_diarization(majors=5, dust=118))
    assert block["speakers_with_30s_plus"] == 5  # still served — one signal of several
    note = block["note"]
    assert "NOT a headcount" in note
    assert "ASK YOUR USER" in note
    assert "num_speakers=N" in note
    assert "transcription is reused" in note
    assert "likely headcount" not in note  # the falsified claim is gone
    assert block["speakers_truncated"] == 111

    exact = dusty_diarization(majors=5, dust=0)
    exact.requested_num_speakers = 5
    block = _summarize_diarization(exact)
    assert "note" not in block and "speakers_with_30s_plus" not in block


def test_implausible_cluster_count_gets_the_stronger_note() -> None:
    """v0.2.4 honesty: >16 unconstrained clusters is called implausible
    outright (a real meeting 'detected' 123), and every amend mention drops
    the falsified 'takes seconds' claim (a real amend took ~12 minutes)."""
    from talkthrough_mcp.core.pipeline import threshold_escalation_note

    note = threshold_escalation_note(dusty_diarization(majors=5, dust=118))
    assert note is not None
    assert "123 speaker clusters" in note
    assert "implausible" in note
    assert "likely over-split" in note
    assert "transcription is reused" in note
    assert "still takes minutes" in note
    assert "takes seconds" not in note
    # v0.2.6 S1 escape hatch: a real 20-person all-hands is not accused
    assert "if that many people really did speak" in note
    assert "confirm it" in note


def test_implausible_note_fires_even_when_every_cluster_is_substantial() -> None:
    """The count alone triggers it: 17 clusters with 30s+ each would silence
    the substantial-count comparison, but 17+ is implausible regardless."""
    from talkthrough_mcp.core.pipeline import threshold_escalation_note

    note = threshold_escalation_note(dusty_diarization(majors=17, dust=0))
    assert note is not None and "implausible" in note


def test_boundary_sixteen_clusters_keeps_the_plain_escalation_text() -> None:
    from talkthrough_mcp.core.pipeline import threshold_escalation_note

    note = threshold_escalation_note(dusty_diarization(majors=10, dust=6))
    assert note is not None
    assert "implausible" not in note
    assert "threshold clustering over-detected" in note
    assert "transcription is reused" in note
    assert "takes seconds" not in note
    assert "really did speak" not in note  # the escape hatch is implausible-only


def test_threshold_escalation_note_is_one_text_on_every_surface() -> None:
    """v0.2.3: the note is a helper both the summary and get_transcript
    serve — byte-identical, and absent outside threshold over-detection."""
    from talkthrough_mcp.core.pipeline import (
        _summarize_diarization,
        threshold_escalation_note,
    )

    dusty = dusty_diarization(majors=5, dust=118)
    note = threshold_escalation_note(dusty)
    assert note is not None
    assert note == _summarize_diarization(dusty)["note"]

    clean = dusty_diarization(majors=3, dust=0)
    assert threshold_escalation_note(clean) is None

    requested = dusty_diarization(majors=5, dust=118)
    requested.requested_num_speakers = 5
    assert threshold_escalation_note(requested) is None

    failed = Diarization(available=False, reason="engine exploded")
    assert threshold_escalation_note(failed) is None


# --- v0.2.6 F2: the escalation note survives a no-op amend ----------------------


def test_escalation_note_survives_a_noop_amend() -> None:
    """Before 0.2.6 ANY requested_num_speakers silenced the note — so the
    exact flow the note itself recommends (ask the user, re-run with k)
    could end with the same dusty roster and NO warning, reading as
    human-confirmed. A no-op amend must keep the warning."""
    from talkthrough_mcp.core.pipeline import (
        _summarize_diarization,
        threshold_escalation_note,
    )

    dusty = dusty_diarization(majors=5, dust=118)
    dusty.requested_num_speakers = 123
    dusty.labels_changed = False
    note = threshold_escalation_note(dusty)
    assert note is not None and "NOT a headcount" in note
    summary = _summarize_diarization(dusty)
    assert summary["note"] == note
    assert summary["labels_changed"] is False

    dusty.labels_changed = True  # the amend DID change labels → user decided
    assert threshold_escalation_note(dusty) is None
    fresh = dusty_diarization(majors=5, dust=118)
    fresh.requested_num_speakers = 123  # fresh k-run (labels_changed None)
    assert threshold_escalation_note(fresh) is None


def test_amend_noop_note_explains_k_and_embedding_model_noops() -> None:
    from talkthrough_mcp.core.pipeline import _summarize_diarization, amend_noop_note

    diarization = dusty_diarization(majors=2, dust=0)
    assert amend_noop_note(diarization) is None  # fresh run: labels_changed unset
    diarization.labels_changed = False
    assert amend_noop_note(diarization) is None  # no explicit k requested
    diarization.requested_num_speakers = 3
    note = amend_noop_note(diarization)
    assert note is not None
    assert "SAME 2" in note and "num_speakers=3" in note
    assert "nothing was relabelled" in note
    assert "a target the clusterer may not reach" in note
    assert _summarize_diarization(diarization)["amend_note"] == note  # one text, every surface

    diarization.requested_num_speakers = None
    diarization.amend_reason = "embedding_model"
    diarization.embedding_model = "new-embedding.onnx"
    model_note = amend_noop_note(diarization)
    assert model_note is not None
    assert "new embedding model new-embedding.onnx" in model_note
    assert "agreed with the stored labels" in model_note
    model_summary = _summarize_diarization(diarization)
    assert model_summary["amend_reason"] == "embedding_model"
    assert model_summary["amend_note"] == model_note

    diarization.requested_num_speakers = 3
    diarization.amend_reason = "both"
    both_note = amend_noop_note(diarization)
    assert both_note is not None
    assert "new embedding model" in both_note and "num_speakers=3" in both_note

    diarization.labels_changed = True
    assert amend_noop_note(diarization) is None
    failed = Diarization(available=False, reason="engine exploded")
    failed.labels_changed = False
    failed.requested_num_speakers = 3
    assert amend_noop_note(failed) is None


# --- v0.2.6 F2+F3: the real amend path measures its own outcome -----------------


class _FakeDiarizer:
    """Stands in for the sherpa engine so the REAL _run_diarization and
    _amend_diarization run end-to-end (snapshot, attribution, provenance)."""

    engine = "fake-engine"
    engine_version = "0.0"
    segmentation_model = "seg-model"
    embedding_model = diarize.DEFAULT_EMBEDDING_MODEL
    threshold = 0.5

    def __init__(self, turns) -> None:  # type: ignore[no-untyped-def]
        self._turns = turns

    def diarize(self, samples, sample_rate, *, num_speakers=None, on_progress=None):  # type: ignore[no-untyped-def]
        return list(self._turns)


def _stored_attributed_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A realistic diarized store entry: roster AND attributed segments,
    saved with a stale tool_versions stamp (the F3 evidence shape)."""
    from talkthrough_mcp.core import jobs
    from talkthrough_mcp.core.diarize import Turn, attribute_segments, speaker_roster
    from talkthrough_mcp.core.manifest import save_manifest

    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path / "home"))
    media = tmp_path / "meeting.mp4"
    media.write_bytes(b"two voices, only ever hashed")
    job_id = jobs.compute_job_id(media)
    manifest = make_manifest(job_id=job_id)
    manifest.media = type(manifest.media)(**{**manifest.media.__dict__, "path": str(media)})
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.segments = attribute_segments(manifest.transcript.segments, turns)
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        embedding_model=diarize.DEFAULT_EMBEDDING_MODEL,
        requested_num_speakers=2,
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=turns,
        produced_by="0.2.2",
    )
    manifest.tool_versions = {"talkthrough-mcp": "0.2.2"}
    directory = jobs.job_dir(job_id)
    directory.mkdir(parents=True)
    save_manifest(manifest, directory)
    return media, turns


def _amend_through_fake_engine(media, monkeypatch: pytest.MonkeyPatch, *, turns, k: int):
    from talkthrough_mcp.core import audio, pipeline

    engine(monkeypatch, available=True)
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", raising=False)
    monkeypatch.setattr(diarize, "create_diarizer", lambda: _FakeDiarizer(turns))
    monkeypatch.setattr(diarize, "load_wav_float32", lambda path: ([], 16000))
    monkeypatch.setattr(audio, "extract_wav", lambda *a, **kw: None)
    return pipeline.process_media(str(media), diarize_speakers=True, num_speakers=k)


def _stored_force_identity_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video: bool = False,
):
    from talkthrough_mcp.core import jobs
    from talkthrough_mcp.core.diarize import Turn, attribute_segments, speaker_roster
    from talkthrough_mcp.core.manifest import save_manifest

    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path / "force-home"))
    media = tmp_path / ("force.mp4" if video else "force.m4a")
    media.write_bytes(b"force fixture bytes")
    job_id = jobs.compute_job_id(media)
    turns = [Turn(0, 4000, "S1"), Turn(4000, 8000, "S2")]
    manifest = make_manifest(job_id=job_id, kind="video" if video else "audio")
    manifest.media = type(manifest.media)(
        **{
            **manifest.media.__dict__,
            "path": str(media),
            "filename": media.name,
            "size_bytes": media.stat().st_size,
        }
    )
    manifest.transcript.segments = attribute_segments(
        manifest.transcript.segments, turns
    )
    manifest.transcript.diarization = Diarization(
        available=True,
        reason="",
        requested_num_speakers=2,
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=turns,
        speaker_names={"S1": "Alice"},
        speaker_name_evidence={"S1": "old introduction"},
        speaker_names_pending_review={"S3": "Carol"},
        speaker_name_evidence_pending_review={"S3": "older roster"},
        speaker_names_pending_review_context={
            "S3": PendingSpeakerReviewContext(longest_turn_at_ms=7000)
        },
    )
    directory = jobs.job_dir(job_id)
    directory.mkdir(parents=True)
    if video:
        frame_directory = directory / "frames"
        frame_directory.mkdir()
        for frame in manifest.frames.items:
            (frame_directory / frame.file).write_bytes(b"old:" + frame.file.encode())
    save_manifest(manifest, directory)
    return media, manifest


def _stub_successful_force(
    media: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video: bool = False,
) -> None:
    from tests.conftest import make_manifest as _make_manifest

    from talkthrough_mcp.core import audio, dedup, frames, ocr, pipeline, stt
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.probe import MediaInfo

    stat = media.stat()
    monkeypatch.setattr(
        pipeline,
        "probe_media",
        lambda path: MediaInfo(
            path=str(media),
            filename=media.name,
            size_bytes=stat.st_size,
            duration_s=8.0,
            has_video=video,
            has_audio=True,
            width=1280 if video else 0,
            height=720 if video else 0,
            video_codec="h264" if video else "",
            mtime_epoch=stat.st_mtime,
        ),
    )
    monkeypatch.setattr(audio, "extract_wav", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda *args, **kwargs: stt.SttResult(
            language="en",
            model="tiny",
            segments=tuple(_make_manifest(kind="audio").transcript.segments[:2]),
            latency_ms=1,
            words=(
                stt.SttWord(0, 2000, "Hello"),
                stt.SttWord(4000, 6000, "There"),
            ),
        ),
    )
    engine(monkeypatch, available=True)
    monkeypatch.setattr(
        diarize,
        "create_diarizer",
        lambda: _FakeDiarizer([Turn(0, 4000, "S1"), Turn(4000, 8000, "S2")]),
    )
    monkeypatch.setattr(diarize, "load_wav_float32", lambda path: ([], 16000))
    monkeypatch.setattr(pipeline, "_tool_versions", lambda: {"talkthrough-mcp": "test"})
    if video:
        def extract_frames(media_path, frame_directory, **kwargs):  # type: ignore[no-untyped-def]
            frame_directory.mkdir(parents=True)
            frame = frames.Frame(ms=0, file="t00000000.jpg")
            (frame_directory / frame.file).write_bytes(b"new frame")
            return [frame], False

        monkeypatch.setattr(frames, "extract_keyframes", extract_frames)
        monkeypatch.setattr(dedup, "mark_duplicates", lambda *args, **kwargs: None)
        monkeypatch.setattr(ocr, "create_engine", lambda **kwargs: object())
        monkeypatch.setattr(ocr, "ocr_image", lambda *args, **kwargs: "Alice")


def test_noop_amend_reports_labels_unchanged_and_keeps_provenance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tester's 353-second no-op: k'≠k re-run converges on the same
    roster. The payload must say labels_changed=false + the noop note, the
    stale transcription stamp must survive, and produced_by must move."""
    from talkthrough_mcp import __version__
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.manifest import save_manifest

    media, turns = _stored_attributed_job(tmp_path, monkeypatch)
    before_amend = jobs.load_job(jobs.compute_job_id(media))
    before_diarization = before_amend.transcript.diarization
    assert before_diarization is not None
    before_diarization.speaker_names = {"S1": "Vera"}
    before_diarization.speaker_name_evidence = {"S1": "intro"}
    before_diarization.speaker_names_pending_review = {"S2": "Tom"}
    before_diarization.speaker_name_evidence_pending_review = {"S2": "old plate"}
    before_diarization.speaker_names_pending_review_context = {
        "S2": PendingSpeakerReviewContext(longest_turn_at_ms=5000)
    }
    save_manifest(before_amend, jobs.job_dir(before_amend.job_id))
    result = _amend_through_fake_engine(media, monkeypatch, turns=turns, k=3)
    assert result.amended is True  # the amend RAN and landed labels…
    diarization = result.manifest.transcript.diarization
    assert diarization is not None
    assert diarization.labels_changed is False  # …but changed nothing, and says so
    assert diarization.amend_reason == "num_speakers"
    assert diarization.speaker_names == {"S1": "Vera"}
    assert diarization.speaker_name_evidence == {"S1": "intro"}
    assert diarization.speaker_names_pending_review == {"S2": "Tom"}
    assert diarization.speaker_name_evidence_pending_review == {"S2": "old plate"}
    assert diarization.speaker_names_pending_review_context == {
        "S2": PendingSpeakerReviewContext(longest_turn_at_ms=5000)
    }
    summary = pipeline.summarize(result)["diarization"]
    assert summary["labels_changed"] is False
    assert "nothing was relabelled" in summary["amend_note"]
    assert "not a constraint" in summary["amend_note"]

    stored = jobs.load_job(result.manifest.job_id)
    assert stored.tool_versions == {"talkthrough-mcp": "0.2.2"}, (
        "an amend must NOT re-stamp transcription provenance"
    )
    stored_diarization = stored.transcript.diarization
    assert stored_diarization is not None
    assert stored_diarization.produced_by == __version__
    assert stored_diarization.labels_changed is False
    assert stored_diarization.amend_reason == "num_speakers"


def test_embedding_model_noop_amend_persists_and_explains_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.manifest import save_manifest

    media, turns = _stored_attributed_job(tmp_path, monkeypatch)
    stored = jobs.load_job(jobs.compute_job_id(media))
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.embedding_model = "old-embedding-model"
    save_manifest(stored, jobs.job_dir(stored.job_id))

    result = _amend_through_fake_engine(media, monkeypatch, turns=turns, k=2)
    amended = result.manifest.transcript.diarization
    assert amended is not None
    assert amended.labels_changed is False
    assert amended.amend_reason == "embedding_model"
    summary = pipeline.summarize(result)["diarization"]
    assert summary["amend_reason"] == "embedding_model"
    assert "new embedding model" in summary["amend_note"]


def test_relabelling_amend_moves_verified_names_to_persistent_pending_review(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.manifest import save_manifest

    media, original = _stored_attributed_job(tmp_path, monkeypatch)
    before_amend = jobs.load_job(jobs.compute_job_id(media))
    before_diarization = before_amend.transcript.diarization
    assert before_diarization is not None
    before_diarization.speaker_names = {"S1": "Stale name"}
    before_diarization.speaker_name_evidence = {"S1": "stale proof"}
    save_manifest(before_amend, jobs.job_dir(before_amend.job_id))
    different = [Turn(0, 3000, "S1"), Turn(3000, 8000, "S2")]  # boundary moved
    result = _amend_through_fake_engine(media, monkeypatch, turns=different, k=3)
    diarization = result.manifest.transcript.diarization
    assert diarization is not None
    assert diarization.labels_changed is True
    assert diarization.speaker_names is None
    assert diarization.speaker_name_evidence is None
    assert diarization.speaker_names_pending_review == {"S1": "Stale name"}
    assert diarization.speaker_name_evidence_pending_review == {"S1": "stale proof"}
    summary = pipeline.summarize(result)["diarization"]
    assert summary["labels_changed"] is True
    assert "amend_note" not in summary
    assert summary["speaker_names_pending_review"] == {"S1": "Stale name"}
    assert summary["speaker_name_evidence_pending_review"] == {"S1": "stale proof"}
    assert "not active identities" in summary["speaker_names_pending_review_note"]

    stored = jobs.load_job(result.manifest.job_id).transcript.diarization
    assert stored is not None
    assert stored.speaker_names_pending_review == {"S1": "Stale name"}

    # A second amend before review has no active names to move; it must retain
    # the existing pending set instead of replacing it with an empty mapping.
    repeated = _amend_through_fake_engine(media, monkeypatch, turns=different, k=2)
    repeated_diarization = repeated.manifest.transcript.diarization
    assert repeated_diarization is not None
    assert repeated_diarization.labels_changed is False
    assert repeated_diarization.speaker_names_pending_review == {"S1": "Stale name"}
    assert repeated_diarization.speaker_name_evidence_pending_review == {
        "S1": "stale proof"
    }

    # Review creates a new active mapping. A later relabel replaces the old
    # pending snapshot with that latest verified mapping instead of growing a
    # history or resurrecting stale evidence.
    from talkthrough_mcp.server import label_speakers

    label_speakers(
        repeated.manifest.job_id,
        {"S1": "Current name"},
        {"S1": "reviewed against current roster"},
    )
    latest = _amend_through_fake_engine(media, monkeypatch, turns=original, k=3)
    latest_diarization = latest.manifest.transcript.diarization
    assert latest_diarization is not None
    assert latest_diarization.labels_changed is True
    assert latest_diarization.speaker_names_pending_review == {"S1": "Current name"}
    assert latest_diarization.speaker_name_evidence_pending_review == {
        "S1": "reviewed against current roster"
    }


def test_multiple_relabel_generations_merge_disjoint_pending_and_keep_old_anchors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.manifest import save_manifest
    from talkthrough_mcp.server import label_speakers

    media, original = _stored_attributed_job(tmp_path, monkeypatch)
    stored = jobs.load_job(jobs.compute_job_id(media))
    first_diarization = stored.transcript.diarization
    assert first_diarization is not None
    first_diarization.speaker_names = {"S1": "Samantha", "S2": "Daniel"}
    first_diarization.speaker_name_evidence = {
        "S1": "first voice",
        "S2": "second voice",
    }
    save_manifest(stored, jobs.job_dir(stored.job_id))

    three_speakers = [
        Turn(0, 2500, "S1"),
        Turn(2500, 5000, "S2"),
        Turn(5000, 8000, "S3"),
    ]
    first_amend = _amend_through_fake_engine(
        media, monkeypatch, turns=three_speakers, k=3
    )
    first_pending = first_amend.manifest.transcript.diarization
    assert first_pending is not None
    assert first_pending.speaker_names_pending_review == {
        "S1": "Samantha",
        "S2": "Daniel",
    }
    assert first_pending.speaker_names_pending_review_context is not None
    assert first_pending.speaker_names_pending_review_context["S1"].talk_time_ms == 5000
    assert first_pending.speaker_names_pending_review_context[
        "S2"
    ].longest_turn_at_ms == 5000

    label_speakers(stored.job_id, {"S3": "Ghost"}, {"S3": "third voice"})
    second_amend = _amend_through_fake_engine(
        media, monkeypatch, turns=original, k=2
    )
    final = second_amend.manifest.transcript.diarization
    assert final is not None
    assert final.speaker_names is None
    assert final.speaker_names_pending_review == {
        "S1": "Samantha",
        "S2": "Daniel",
        "S3": "Ghost",
    }
    assert final.speaker_names_pending_review_context is not None
    assert set(final.speaker_names_pending_review_context) == {"S1", "S2", "S3"}
    assert final.speaker_names_pending_review_context[
        "S3"
    ].source_detected_num_speakers == 3
    assert final.speaker_names_pending_review_context["S3"].longest_turn_at_ms == 5000
    summary = pipeline.summarize(second_amend)["diarization"]
    assert summary["speaker_names_pending_review_stale_labels"] == ["S3"]

    label_speakers(stored.job_id, {"S3": None})
    cleaned = jobs.load_job(stored.job_id).transcript.diarization
    assert cleaned is not None
    assert cleaned.speaker_names_pending_review == {
        "S1": "Samantha",
        "S2": "Daniel",
    }
    assert cleaned.speaker_names_pending_review_context is not None
    assert set(cleaned.speaker_names_pending_review_context) == {"S1", "S2"}


def test_same_label_collision_is_reported_once_and_active_value_wins(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.manifest import save_manifest

    media, _original = _stored_attributed_job(tmp_path, monkeypatch)
    stored = jobs.load_job(jobs.compute_job_id(media))
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.speaker_names = {"S1": "New Alice"}
    diarization.speaker_name_evidence = {"S1": "fresh intro"}
    diarization.speaker_names_pending_review = {"S1": "Old Alice"}
    diarization.speaker_name_evidence_pending_review = {"S1": "old intro"}
    save_manifest(stored, jobs.job_dir(stored.job_id))

    changed = [Turn(0, 3000, "S1"), Turn(3000, 8000, "S2")]
    result = _amend_through_fake_engine(media, monkeypatch, turns=changed, k=3)
    updated = result.manifest.transcript.diarization
    assert updated is not None
    assert updated.speaker_names_pending_review == {"S1": "New Alice"}
    summary = pipeline.summarize(result)["diarization"]
    assert summary["speaker_names_pending_review_dropped"] == {"S1": "Old Alice"}
    assert summary["speaker_names_pending_review_dropped_total"] == 1
    assert "superseded" in summary["speaker_names_pending_review_dropped_note"]

    reloaded = jobs.load_job(stored.job_id)
    later = pipeline.summarize(
        pipeline.ProcessResult(manifest=reloaded, reused=True, elapsed_s=0)
    )["diarization"]
    assert "speaker_names_pending_review_dropped" not in later


def test_same_label_and_name_does_not_report_a_drop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.manifest import save_manifest

    media, _original = _stored_attributed_job(tmp_path, monkeypatch)
    stored = jobs.load_job(jobs.compute_job_id(media))
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.speaker_names = {"S1": "Alice"}
    diarization.speaker_names_pending_review = {"S1": "Alice"}
    save_manifest(stored, jobs.job_dir(stored.job_id))

    changed = [Turn(0, 3000, "S1"), Turn(3000, 8000, "S2")]
    result = _amend_through_fake_engine(media, monkeypatch, turns=changed, k=3)
    summary = pipeline.summarize(result)["diarization"]
    assert summary["speaker_names_pending_review"] == {"S1": "Alice"}
    assert "speaker_names_pending_review_dropped" not in summary


@pytest.mark.parametrize("diarize_flag", [False, None])
def test_force_on_named_job_without_resolved_diarization_fails_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diarize_flag: bool | None,
) -> None:
    from talkthrough_mcp.core import jobs, pipeline

    media, stored = _stored_force_identity_job(tmp_path, monkeypatch)
    engine(monkeypatch, available=True)
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    before = (jobs.job_dir(stored.job_id) / "manifest.json").read_bytes()

    def no_probe(path: Path):  # type: ignore[no-untyped-def]
        raise AssertionError("unsafe force must fail before probing or staging")

    monkeypatch.setattr(pipeline, "probe_media", no_probe)
    with pytest.raises(ValidationError, match=r"force=true, diarize=true"):
        pipeline.process_media(
            str(media),
            force=True,
            diarize_speakers=diarize_flag,
        )
    assert (jobs.job_dir(stored.job_id) / "manifest.json").read_bytes() == before
    assert not list(jobs.job_dir(stored.job_id).glob(f"{jobs.REPROCESS_PREFIX}*"))


def test_successful_named_force_moves_all_identities_to_pending_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline

    media, stored = _stored_force_identity_job(tmp_path, monkeypatch)
    _stub_successful_force(media, monkeypatch)
    result = pipeline.process_media(
        str(media), force=True, diarize_speakers=True, num_speakers=2
    )
    rebuilt = result.manifest.transcript.diarization
    assert rebuilt is not None and rebuilt.available
    assert rebuilt.speaker_names is None
    assert rebuilt.speaker_name_evidence is None
    assert rebuilt.speaker_names_pending_review == {
        "S1": "Alice",
        "S3": "Carol",
    }
    assert rebuilt.speaker_name_evidence_pending_review == {
        "S1": "old introduction",
        "S3": "older roster",
    }
    assert rebuilt.speaker_names_pending_review_context is not None
    assert set(rebuilt.speaker_names_pending_review_context) == {"S1", "S3"}
    summary = pipeline.summarize(result)["diarization"]
    assert "rebuilt the transcript and speaker labels" in summary[
        "force_identity_review_note"
    ]
    assert "not as an active identity" in summary["force_identity_review_note"]
    assert not list(jobs.job_dir(stored.job_id).glob(f"{jobs.REPROCESS_PREFIX}*"))


def test_named_force_same_label_collision_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import jobs, pipeline
    from talkthrough_mcp.core.manifest import save_manifest

    media, stored = _stored_force_identity_job(tmp_path, monkeypatch)
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.speaker_names_pending_review = {"S1": "Older Alice"}
    diarization.speaker_name_evidence_pending_review = {"S1": "old proof"}
    diarization.speaker_names_pending_review_context = None
    save_manifest(stored, jobs.job_dir(stored.job_id))
    _stub_successful_force(media, monkeypatch)

    result = pipeline.process_media(
        str(media), force=True, diarize_speakers=True, num_speakers=2
    )
    summary = pipeline.summarize(result)["diarization"]
    assert summary["speaker_names_pending_review"] == {"S1": "Alice"}
    assert summary["speaker_names_pending_review_dropped"] == {
        "S1": "Older Alice"
    }
    later = pipeline.summarize(
        pipeline.ProcessResult(
            manifest=jobs.load_job(stored.job_id), reused=True, elapsed_s=0
        )
    )["diarization"]
    assert "speaker_names_pending_review_dropped" not in later


@pytest.mark.parametrize("failure_stage", ["stt", "diarization", "ocr"])
def test_failed_named_force_keeps_old_manifest_and_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from talkthrough_mcp.core import jobs, ocr, pipeline, stt

    video = failure_stage == "ocr"
    media, stored = _stored_force_identity_job(
        tmp_path, monkeypatch, video=video
    )
    _stub_successful_force(media, monkeypatch, video=video)
    directory = jobs.job_dir(stored.job_id)
    before_manifest = (directory / "manifest.json").read_bytes()
    before_frames = (
        {
            path.name: path.read_bytes()
            for path in (directory / "frames").iterdir()
        }
        if video
        else {}
    )
    if failure_stage == "stt":
        monkeypatch.setattr(
            stt,
            "transcribe",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ToolFailureError("controlled STT failure")
            ),
        )
    elif failure_stage == "diarization":
        monkeypatch.setattr(
            diarize,
            "create_diarizer",
            lambda: (_ for _ in ()).throw(
                ToolFailureError("controlled diarizer failure")
            ),
        )
    else:
        monkeypatch.setattr(
            ocr,
            "ocr_image",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ToolFailureError("controlled OCR failure")
            ),
        )

    with pytest.raises(ToolFailureError):
        pipeline.process_media(
            str(media), force=True, diarize_speakers=True, num_speakers=2
        )
    assert (directory / "manifest.json").read_bytes() == before_manifest
    if video:
        assert {
            path.name: path.read_bytes()
            for path in (directory / "frames").iterdir()
        } == before_frames
    assert jobs.load_job(stored.job_id).transcript.diarization is not None
    assert not list(directory.glob(f"{jobs.REPROCESS_PREFIX}*"))


def test_two_named_force_calls_serialize_under_the_job_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from talkthrough_mcp.core import pipeline, stt

    media, _stored = _stored_force_identity_job(tmp_path, monkeypatch)
    _stub_successful_force(media, monkeypatch)
    real_transcribe = stt.transcribe
    guard = threading.Lock()
    active = 0
    max_active = 0

    def observed_transcribe(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return real_transcribe(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(stt, "transcribe", observed_transcribe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(
                pipeline.process_media,
                str(media),
                force=True,
                diarize_speakers=True,
                num_speakers=2,
            )
            for _ in range(2)
        ]
        assert all(call.result(timeout=10).reused is False for call in calls)
    assert max_active == 1


def test_concurrent_label_and_relabel_amend_never_lose_verified_names(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import audio, jobs, pipeline
    from talkthrough_mcp.core.diarize import Turn
    from talkthrough_mcp.core.manifest import save_manifest
    from talkthrough_mcp.server import label_speakers

    media, _turns = _stored_attributed_job(tmp_path, monkeypatch)
    stored = jobs.load_job(jobs.compute_job_id(media))
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.speaker_names = {"S1": "Vera"}
    diarization.speaker_name_evidence = {"S1": "original intro"}
    save_manifest(stored, jobs.job_dir(stored.job_id))

    different = [Turn(0, 3000, "S1"), Turn(3000, 8000, "S2")]
    engine(monkeypatch, available=True)
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", raising=False)
    monkeypatch.setattr(diarize, "create_diarizer", lambda: _FakeDiarizer(different))
    monkeypatch.setattr(diarize, "load_wav_float32", lambda path: ([], 16000))
    monkeypatch.setattr(audio, "extract_wav", lambda *args, **kwargs: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        amend = pool.submit(
            pipeline.process_media,
            str(media),
            diarize_speakers=True,
            num_speakers=3,
        )
        label = pool.submit(
            label_speakers,
            stored.job_id,
            {"S2": "Tom"},
            {"S2": "current name plate"},
        )
        amend.result(timeout=10)
        label.result(timeout=10)

    final = jobs.load_job(stored.job_id).transcript.diarization
    assert final is not None
    combined = {
        **(final.speaker_names_pending_review or {}),
        **(final.speaker_names or {}),
    }
    assert combined == {"S1": "Vera", "S2": "Tom"}
    combined_evidence = {
        **(final.speaker_name_evidence_pending_review or {}),
        **(final.speaker_name_evidence or {}),
    }
    assert combined_evidence == {
        "S1": "original intro",
        "S2": "current name plate",
    }


# --- longest-turn roster anchor + duration (v0.3.0) -----------------------------


def test_roster_payload_carries_longest_turn_anchor() -> None:
    """label → t0_ms of the speaker's longest turn; equal durations resolve
    to the EARLIEST turn — deterministic regardless of stored turn order."""
    from talkthrough_mcp.core.diarize import Turn, speaker_roster
    from talkthrough_mcp.core.pipeline import roster_payload

    turns = [
        Turn(0, 4000, "S1"),
        Turn(4000, 5000, "S2"),
        Turn(5000, 9000, "S1"),  # ties the first S1 turn at 4000 ms
        Turn(9000, 20000, "S2"),
    ]
    diarization = Diarization(
        available=True,
        reason="",
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=turns,
    )
    entries, hidden = roster_payload(diarization)
    assert hidden == 0
    by_label = {entry["label"]: entry for entry in entries}
    assert by_label["S1"]["longest_turn_at_ms"] == 0  # 4000ms tie → earliest t0
    assert by_label["S1"]["longest_turn_duration_ms"] == 4000
    assert by_label["S1"]["longest_turn_ms"] == 0  # deprecated alias through 0.3.x
    assert by_label["S2"]["longest_turn_at_ms"] == 9000  # 11000ms beats 1000ms
    assert by_label["S2"]["longest_turn_duration_ms"] == 11_000
    assert by_label["S2"]["longest_turn_ms"] == 9000

    shuffled = Diarization(
        available=True,
        reason="",
        detected_num_speakers=2,
        speakers=speaker_roster(turns),
        turns=list(reversed(turns)),
    )
    shuffled_entries, _ = roster_payload(shuffled)
    assert shuffled_entries == entries, "tie-break must not depend on turn order"

    no_turns = dusty_diarization(majors=2, dust=0)
    bare_entries, _ = roster_payload(no_turns)
    assert all("longest_turn_at_ms" not in entry for entry in bare_entries)
    assert all("longest_turn_duration_ms" not in entry for entry in bare_entries)
    assert all("longest_turn_ms" not in entry for entry in bare_entries)
