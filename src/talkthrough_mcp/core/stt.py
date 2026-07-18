"""Local STT with per-segment timestamps via faster-whisper.

Privacy contract: transcription is local-only — audio never leaves the
machine. Models are referenced by NAME (``tiny``/``base``/``small``/
``medium``/``large-v3``) and auto-download once from Hugging Face into the
local cache; there is no cloud STT path in this codebase.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
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
    vocabulary_echo_trimmed: int = 0  # opening initial_prompt echoes dropped

    def full_text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text).strip()


# --- vocabulary echo (initial_prompt replayed over quiet openings) -----------

VOCAB_ECHO_WINDOW_MS = 90_000
VOCAB_ECHO_MIN_VOCAB_FRACTION = 0.8
VOCAB_ECHO_MIN_REPEATS = 3
VOCAB_ECHO_MIN_PREFIX_TOKENS = 3


def _echo_tokens(text: str) -> list[str]:
    """Fold to comparison tokens: casefold + ё→е, punctuation-split."""
    return [t for t in re.split(r"[\W_]+", text.casefold().replace("ё", "е")) if t]


def trim_vocabulary_echo(
    segments: Sequence[SttSegment], vocabulary: str
) -> tuple[list[SttSegment], list[SttSegment]]:
    """Drop opening segments that are the ``initial_prompt`` echoed back.

    Whisper replays the vocabulary over quiet opening seconds (a known
    ``initial_prompt`` trait, documented in MODEL-NOTES since v0.2.1) — on a
    real meeting the echo swallowed the actual opening words. A segment
    inside the first ~90 s is treated as echo when (a) at least ~80% of its
    tokens come from the vocabulary AND (b) one token repeats 3+ times OR
    the text is a near-verbatim prefix of the vocabulary itself. A live
    roll-call ("на встрече присутствуют Анастасия, Диана и Влад") carries
    verbs/prepositions, fails (a), and survives.

    Returns ``(kept, trimmed)``; timing of kept segments is untouched.
    """
    vocab_tokens = _echo_tokens(vocabulary)
    if not vocab_tokens:
        return list(segments), []
    vocab_set = set(vocab_tokens)
    kept: list[SttSegment] = []
    trimmed: list[SttSegment] = []
    for segment in segments:
        if segment.t0_ms >= VOCAB_ECHO_WINDOW_MS:
            kept.append(segment)
            continue
        tokens = _echo_tokens(segment.text)
        if not tokens:
            kept.append(segment)
            continue
        vocab_fraction = sum(1 for t in tokens if t in vocab_set) / len(tokens)
        if vocab_fraction < VOCAB_ECHO_MIN_VOCAB_FRACTION:
            kept.append(segment)
            continue
        max_repeat = max(tokens.count(t) for t in set(tokens))
        prefix = vocab_tokens[: len(tokens)]
        near_verbatim_prefix = (
            len(tokens) >= VOCAB_ECHO_MIN_PREFIX_TOKENS
            and len(tokens) == len(prefix)
            and sum(1 for got, want in zip(tokens, prefix, strict=True) if got != want) <= 1
        )
        if max_repeat >= VOCAB_ECHO_MIN_REPEATS or near_verbatim_prefix:
            trimmed.append(segment)
        else:
            kept.append(segment)
    return kept, trimmed


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

    echo_trimmed = 0
    if vocabulary:
        segments, trimmed = trim_vocabulary_echo(segments, vocabulary)
        echo_trimmed = len(trimmed)
        for segment in trimmed:
            logger.info(
                "dropped vocabulary-echo segment [%d-%d ms]: %r",
                segment.t0_ms,
                segment.t1_ms,
                segment.text,
            )

    probability = getattr(info, "language_probability", None)
    return SttResult(
        language=getattr(info, "language", None),
        model=model_name,
        segments=tuple(_renumber(segments)),
        latency_ms=int((time.monotonic() - started) * 1000),
        language_probability=round(float(probability), 3) if probability is not None else None,
        vocabulary_echo_trimmed=echo_trimmed,
    )
