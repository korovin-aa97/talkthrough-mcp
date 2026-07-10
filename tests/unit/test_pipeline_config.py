"""Whisper model resolution: per-call override, env default, allowlist."""

from __future__ import annotations

import pytest

from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.pipeline import (
    ALLOWED_WHISPER_MODELS,
    DEFAULT_WHISPER_MODEL,
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
