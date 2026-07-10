"""RapidOCR wrapper with graceful disable.

OCR is on by default and pip-only (``rapidocr`` + onnxruntime — no system
packages). ``TALKTHROUGH_OCR=off`` disables it; an import or engine failure
degrades to "no OCR" with a logged reason instead of failing the pipeline.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class OcrEngine(Protocol):  # pragma: no cover - typing only
    def __call__(self, content: str) -> Any: ...


def ocr_enabled() -> bool:
    return os.environ.get("TALKTHROUGH_OCR", "on").strip().lower() not in {"off", "0", "false"}


def create_engine() -> OcrEngine | None:
    """Build a RapidOCR engine, or None when OCR is disabled/unavailable."""
    if not ocr_enabled():
        logger.info("OCR disabled via TALKTHROUGH_OCR")
        return None
    try:
        for noisy in ("rapidocr", "RapidOCR"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        from rapidocr import RapidOCR

        # First use may download ONNX models; keep any stray stdout out of
        # the MCP stdio channel.
        with contextlib.redirect_stdout(sys.stderr):
            engine: OcrEngine = RapidOCR()
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
