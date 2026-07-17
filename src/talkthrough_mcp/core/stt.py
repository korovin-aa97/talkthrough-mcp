"""Local STT with per-segment timestamps via faster-whisper.

Privacy contract: transcription is local-only — audio never leaves the
machine. Models are referenced by NAME (``tiny``/``base``/``small``/
``medium``/``large-v3``) and auto-download once from Hugging Face into the
local cache; there is no cloud STT path in this codebase.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SttSegment:
    seq: int
    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None  # "S1"/"S2"/… once diarized; never serialized as null


@dataclass(frozen=True)
class SttResult:
    language: str | None
    model: str
    segments: tuple[SttSegment, ...]
    latency_ms: int
    language_probability: float | None = None

    def full_text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text).strip()


def _load_model(model_name: str) -> Any:
    """Load whisper from the LOCAL cache first; touch the network only on a miss.

    Without ``local_files_only`` huggingface_hub revalidates repo metadata
    against huggingface.co on EVERY model load even when fully cached — which
    would break the "no runtime network beyond one-time downloads" promise
    (and any offline machine). Cache-first keeps warm loads at zero network.
    """
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model_name, device="cpu", compute_type="int8", local_files_only=True)
    except Exception:
        logger.info("whisper model %r not in local cache — downloading once", model_name)
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def _renumber(segments: list[SttSegment]) -> list[SttSegment]:
    return [replace(segment, seq=index) for index, segment in enumerate(segments, start=1)]


def transcribe(
    wav_path: Path,
    *,
    model_name: str,
    language: str | None = None,
    vocabulary: str | None = None,
    on_segment: Callable[[int], None] | None = None,
) -> SttResult:
    """Transcribe a 16 kHz mono WAV into ordered, renumbered ms segments.

    ``vocabulary`` becomes the whisper ``initial_prompt`` — feed it product
    names and jargon so they survive transcription. ``on_segment`` receives
    the end-ms of each decoded segment (progress reporting hook).
    """
    started = time.monotonic()
    model = _load_model(model_name)

    transcribe_kwargs: dict[str, Any] = {"vad_filter": True}
    if vocabulary:
        transcribe_kwargs["initial_prompt"] = vocabulary
    if language:
        transcribe_kwargs["language"] = language

    raw_segments, info = model.transcribe(str(wav_path), **transcribe_kwargs)
    segments: list[SttSegment] = []
    for index, segment in enumerate(raw_segments, start=1):
        text = segment.text.strip()
        if not text:
            continue
        t1_ms = max(0, int(segment.end * 1000))
        segments.append(
            SttSegment(
                seq=index,
                t0_ms=max(0, int(segment.start * 1000)),
                t1_ms=t1_ms,
                text=text,
            )
        )
        if on_segment is not None:
            on_segment(t1_ms)

    probability = getattr(info, "language_probability", None)
    return SttResult(
        language=getattr(info, "language", None),
        model=model_name,
        segments=tuple(_renumber(segments)),
        latency_ms=int((time.monotonic() - started) * 1000),
        language_probability=round(float(probability), 3) if probability is not None else None,
    )
