"""Whisper model resolution + diarize request matrix + amend gating."""

from __future__ import annotations

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import diarize
from talkthrough_mcp.core.diarize import Diarization
from talkthrough_mcp.core.errors import ValidationError
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
    assert diarize.resolved_embedding_label().endswith("/models/x.onnx")
    assert "~" not in diarize.resolved_embedding_label()


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
    monkeypatch.setattr(audio, "extract_wav", lambda *a, **k: None)

    def fake_run(wav_path, transcript, request, report) -> None:
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
    from talkthrough_mcp.core import pipeline

    media = _stored_job(tmp_path, monkeypatch)
    result = _run_amend(media, monkeypatch, succeed=False)
    assert result.reused is True
    assert result.amended is False, "a failed amend must not be reported as applied"
    summary = pipeline.summarize(result)
    assert "diarization_amended" not in summary
    assert summary["diarization"]["available"] is False
    assert "TLS" in summary["diarization"]["reason"]


def test_successful_amend_still_reports_the_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from talkthrough_mcp.core import pipeline

    media = _stored_job(tmp_path, monkeypatch)
    result = _run_amend(media, monkeypatch, succeed=True)
    assert result.amended is True
    assert pipeline.summarize(result)["diarization_amended"] is True


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
    agent to ASK THE USER and names the fast num_speakers amend."""
    from talkthrough_mcp.core.pipeline import _summarize_diarization

    block = _summarize_diarization(dusty_diarization(majors=5, dust=118))
    assert block["speakers_with_30s_plus"] == 5  # still served — one signal of several
    note = block["note"]
    assert "NOT a headcount" in note
    assert "ASK YOUR USER" in note
    assert "num_speakers=N" in note
    assert "whisper is not re-run" in note
    assert "likely headcount" not in note  # the falsified claim is gone
    assert block["speakers_truncated"] == 111

    exact = dusty_diarization(majors=5, dust=0)
    exact.requested_num_speakers = 5
    block = _summarize_diarization(exact)
    assert "note" not in block and "speakers_with_30s_plus" not in block
