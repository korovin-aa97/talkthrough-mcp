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
