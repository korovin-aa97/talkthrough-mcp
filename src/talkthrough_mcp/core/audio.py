"""Audio extraction: anything ffmpeg can read -> 16 kHz mono PCM WAV for STT."""

from __future__ import annotations

from pathlib import Path

from .ffmpeg import ffmpeg_path, run_tool


def extract_wav(media: Path, wav_path: Path, *, timeout: int) -> None:
    run_tool(
        [
            ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        timeout=timeout,
    )
