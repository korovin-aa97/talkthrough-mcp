"""RapidOCR wrapper with graceful disable.

OCR is on by default and pip-only (``rapidocr`` + onnxruntime — no system
packages). ``TALKTHROUGH_OCR=off`` disables it; an import or engine failure
degrades to "no OCR" with a logged reason instead of failing the pipeline.

Script selection (issue #3): the default RapidOCR models cover Latin +
Chinese. ``TALKTHROUGH_OCR_LANG`` picks the recognition pack for other
scripts — it accepts either a narration-style language code (``ru``, ``ja``,
``ko``, ``ar``, ``hi``, …) or a raw RapidOCR pack name (``eslav``,
``cyrillic``, ``latin``, …). ``TALKTHROUGH_OCR_PARAMS`` is the advanced
escape hatch: a JSON object of raw RapidOCR constructor params, merged over
the derived ones.

Auto-selection (v0.2.1): STT detects the narration language BEFORE the OCR
stage runs, so when ``TALKTHROUGH_OCR_LANG`` is NOT set and the detected
language maps to a script pack in ``_LANG_ALIASES``, that pack becomes the
derived default — Russian narration stops producing unreadable Cyrillic
frames out of the box. The explicit env always wins, and languages outside
the alias table (Latin-script es/fr/de/en …) keep the stock engine.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Narration-style language codes → RapidOCR recognition packs. Codes the
# PP-OCRv6 multilingual model already covers (en, ch, japan, chinese_cht and
# the Latin-script languages like es/fr/de) pass through untouched.
_LANG_ALIASES: dict[str, str] = {
    "ru": "eslav",
    "uk": "eslav",
    "be": "eslav",
    "bg": "cyrillic",
    "sr": "cyrillic",
    "mk": "cyrillic",
    "mn": "cyrillic",
    "kk": "cyrillic",
    "ko": "korean",
    "ar": "arabic",
    "fa": "arabic",
    "ur": "arabic",
    "hi": "devanagari",
    "mr": "devanagari",
    "ne": "devanagari",
    "zh": "ch",
    "zh-hant": "chinese_cht",
    "ja": "japan",
}

# Scripts that only ship as PP-OCRv5 mobile recognition models (the default
# PP-OCRv6 line covers Latin + ch/en/japan). Detection stays at the default
# model — it finds text boxes fine across scripts; recognition is what needs
# the per-script pack.
_V5_REC_PACKS = frozenset(
    {"arabic", "cyrillic", "devanagari", "el", "eslav", "korean", "latin", "ta", "te", "th"}
)


class OcrEngine(Protocol):  # pragma: no cover - typing only
    def __call__(self, content: str) -> Any: ...


def ocr_enabled() -> bool:
    return os.environ.get("TALKTHROUGH_OCR", "on").strip().lower() not in {"off", "0", "false"}


def engine_params(language_hint: str | None = None) -> dict[str, Any]:
    """RapidOCR constructor params from the TALKTHROUGH_OCR_* env vars.

    ``language_hint`` is the STT-detected narration language; it applies only
    when ``TALKTHROUGH_OCR_LANG`` is empty AND the hint is a known alias —
    unknown or Latin-script codes never switch packs (the stock engine
    already reads Latin, and an invalid pack must not reach the engine).
    """
    params: dict[str, Any] = {}
    lang = os.environ.get("TALKTHROUGH_OCR_LANG", "").strip().lower()
    if not lang and language_hint:
        hint = language_hint.strip().lower()
        if hint in _LANG_ALIASES:
            lang = hint
            logger.info(
                "OCR pack %r derived from detected speech language %r "
                "(TALKTHROUGH_OCR_LANG overrides; pack models are a one-time download)",
                _LANG_ALIASES[hint],
                hint,
            )
    if lang:
        pack = _LANG_ALIASES.get(lang, lang)
        params["Rec.lang_type"] = pack
        if pack in _V5_REC_PACKS:
            params["Rec.ocr_version"] = "PP-OCRv5"
            params["Rec.model_type"] = "mobile"
    raw = os.environ.get("TALKTHROUGH_OCR_PARAMS", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if not isinstance(overrides, dict):
                raise ValueError("expected a JSON object")
        except ValueError as exc:
            logger.warning("ignoring invalid TALKTHROUGH_OCR_PARAMS: %s", exc)
        else:
            params.update(overrides)
    return params


def _coerce_params(params: dict[str, Any]) -> dict[str, Any]:
    """Turn string values into the enums RapidOCR expects; drop invalid keys."""
    from rapidocr import EngineType, LangCls, LangDet, LangRec, ModelType, OCRVersion

    lang_enums: dict[str, Any] = {"Det": LangDet, "Cls": LangCls, "Rec": LangRec}
    field_enums: dict[str, Any] = {
        "engine_type": EngineType,
        "model_type": ModelType,
        "ocr_version": OCRVersion,
    }
    coerced: dict[str, Any] = {}
    for key, value in params.items():
        section, _, field = key.partition(".")
        enum_cls = lang_enums.get(section) if field == "lang_type" else field_enums.get(field)
        if enum_cls is not None and isinstance(value, str):
            try:
                value = enum_cls(value)
            except ValueError:
                allowed = ", ".join(e.value for e in enum_cls)
                logger.warning("dropping %s=%r (allowed: %s)", key, value, allowed)
                continue
        coerced[key] = value
    return coerced


def create_engine(language_hint: str | None = None) -> OcrEngine | None:
    """Build a RapidOCR engine, or None when OCR is disabled/unavailable."""
    if not ocr_enabled():
        logger.info("OCR disabled via TALKTHROUGH_OCR")
        return None
    try:
        for noisy in ("rapidocr", "RapidOCR"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        from rapidocr import RapidOCR

        params = _coerce_params(engine_params(language_hint))
        if params:
            logger.info("OCR params: %s", {k: str(v) for k, v in params.items()})
        # First use may download ONNX models; keep any stray stdout out of
        # the MCP stdio channel.
        with contextlib.redirect_stdout(sys.stderr):
            engine: OcrEngine = RapidOCR(params=params) if params else RapidOCR()
        return engine
    except Exception as exc:
        logger.warning("OCR unavailable, continuing without it: %s", exc)
        return None


def ocr_image(engine: OcrEngine, path: Path) -> str:
    """Joined text found on one frame; empty string when nothing is recognized."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = engine(str(path))
    except Exception as exc:
        logger.warning("OCR failed on %s: %s", path.name, exc)
        return ""
    texts = getattr(result, "txts", None) or ()
    return " ".join(str(text).strip() for text in texts if str(text).strip())
