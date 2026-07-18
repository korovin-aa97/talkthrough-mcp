"""TALKTHROUGH_OCR_LANG / TALKTHROUGH_OCR_PARAMS plumbing (issue #3)."""

from __future__ import annotations

import pytest
from rapidocr import LangRec, ModelType, OCRVersion

from talkthrough_mcp.core.ocr import _coerce_params, engine_params

RU_PARAMS = {
    "Rec.lang_type": "eslav",
    "Rec.ocr_version": "PP-OCRv5",
    "Rec.model_type": "mobile",
}


def test_no_env_means_no_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_OCR_LANG", raising=False)
    monkeypatch.delenv("TALKTHROUGH_OCR_PARAMS", raising=False)
    assert engine_params() == {}


def test_ru_maps_to_the_v5_eslav_recognition_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "ru")
    assert engine_params() == RU_PARAMS


def test_v6_covered_codes_pass_through_without_version_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "en")
    assert engine_params() == {"Rec.lang_type": "en"}
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "zh")
    assert engine_params() == {"Rec.lang_type": "ch"}
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "es")
    assert engine_params() == {"Rec.lang_type": "es"}
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "ja")
    assert engine_params() == {"Rec.lang_type": "japan"}


def test_raw_v5_pack_names_get_the_version_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "cyrillic")
    params = engine_params()
    assert params["Rec.lang_type"] == "cyrillic"
    assert params["Rec.ocr_version"] == "PP-OCRv5"
    assert params["Rec.model_type"] == "mobile"


def test_params_env_merges_and_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "ru")
    monkeypatch.setenv(
        "TALKTHROUGH_OCR_PARAMS", '{"Rec.lang_type": "cyrillic", "Global.text_score": 0.3}'
    )
    params = engine_params()
    assert params["Rec.lang_type"] == "cyrillic"
    assert params["Global.text_score"] == 0.3
    assert params["Rec.ocr_version"] == "PP-OCRv5"


def test_invalid_params_json_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "ru")
    monkeypatch.setenv("TALKTHROUGH_OCR_PARAMS", "not json")
    assert engine_params() == RU_PARAMS


def test_coerce_turns_strings_into_rapidocr_enums() -> None:
    coerced = _coerce_params(dict(RU_PARAMS))
    assert coerced["Rec.lang_type"] is LangRec("eslav")
    assert coerced["Rec.ocr_version"] is OCRVersion("PP-OCRv5")
    assert coerced["Rec.model_type"] is ModelType("mobile")


def test_coerce_drops_unknown_enum_values() -> None:
    coerced = _coerce_params({"Rec.lang_type": "martian", "Global.text_score": 0.5})
    assert "Rec.lang_type" not in coerced
    assert coerced["Global.text_score"] == 0.5


# --- auto-selection from the detected speech language (v0.2.1) ---------------


def test_detected_language_hint_derives_the_pack_when_env_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALKTHROUGH_OCR_LANG", raising=False)
    monkeypatch.delenv("TALKTHROUGH_OCR_PARAMS", raising=False)
    assert engine_params("ru") == RU_PARAMS
    assert engine_params("ja") == {"Rec.lang_type": "japan"}
    assert engine_params("KO ") == {
        "Rec.lang_type": "korean",
        "Rec.ocr_version": "PP-OCRv5",
        "Rec.model_type": "mobile",
    }


def test_explicit_env_always_beats_the_detected_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TALKTHROUGH_OCR_LANG", "ja")
    assert engine_params("ru") == {"Rec.lang_type": "japan"}


def test_latin_script_languages_never_trigger_a_pack_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANDATORY guard: es/fr/de/en are not in _LANG_ALIASES and must keep the
    stock engine — an invalid Rec.lang_type must never even be derived (it
    would only be dropped later by _coerce_params, which is too late to rely
    on)."""
    monkeypatch.delenv("TALKTHROUGH_OCR_LANG", raising=False)
    monkeypatch.delenv("TALKTHROUGH_OCR_PARAMS", raising=False)
    for detected in ("es", "fr", "de", "en", "pt", "it"):
        assert engine_params(detected) == {}, detected


def test_unknown_or_absent_hints_leave_the_engine_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALKTHROUGH_OCR_LANG", raising=False)
    monkeypatch.delenv("TALKTHROUGH_OCR_PARAMS", raising=False)
    assert engine_params(None) == {}
    assert engine_params("") == {}
    assert engine_params("xx") == {}


def test_params_env_still_merges_over_the_derived_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALKTHROUGH_OCR_LANG", raising=False)
    monkeypatch.setenv("TALKTHROUGH_OCR_PARAMS", '{"Global.text_score": 0.3}')
    params = engine_params("ru")
    assert params["Rec.lang_type"] == "eslav"
    assert params["Global.text_score"] == 0.3
