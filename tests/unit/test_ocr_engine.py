"""The engine call contract: the frame path reaches RapidOCR positionally.

RapidOCR names its first parameter ``img_content``, so a keyword call would
raise ``TypeError`` against the real engine — and ``ocr_image`` swallows
exceptions, so the regression would surface as silently empty OCR text on
every frame rather than as a crash. ``OcrEngine`` declares the argument
positional-only to pin that down; rapidocr 3.9.2 shipping ``py.typed`` is
what first made the mismatch visible to mypy.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from talkthrough_mcp.core.ocr import OcrEngine, ocr_image


class _RapidOcrLike:
    """Mirrors the real ``RapidOCR.__call__`` parameter name and defaults."""

    def __init__(self, *texts: str) -> None:
        self._texts = texts
        self.seen: list[str] = []

    def __call__(self, img_content: str, use_det: bool | None = None) -> Any:
        self.seen.append(img_content)
        return SimpleNamespace(txts=self._texts)


def test_protocol_declares_the_frame_path_positional_only() -> None:
    parameters = list(inspect.signature(OcrEngine.__call__).parameters.values())
    assert parameters[1].kind is inspect.Parameter.POSITIONAL_ONLY


def test_ocr_image_calls_an_img_content_engine_positionally(tmp_path: Path) -> None:
    frame = tmp_path / "frame-000001.jpg"
    frame.touch()
    engine = _RapidOcrLike("  Build failed  ", "", "exit code 1")

    assert ocr_image(engine, frame) == "Build failed exit code 1"
    assert engine.seen == [str(frame)]
