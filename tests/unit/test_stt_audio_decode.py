"""Exercise the real faster-whisper/PyAV boundary without a Whisper model."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np
from faster_whisper.audio import decode_audio


def test_whisper_decodes_local_pcm_wav(tmp_path: Path) -> None:
    """Catch incompatible av.open arguments even in a fresh wheel install."""
    wav_path = tmp_path / "audio.wav"
    sample_count = 1600
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack("<h", 8192) * sample_count)

    audio = decode_audio(str(wav_path), sampling_rate=16000)

    assert audio.dtype == np.float32
    assert audio.shape == (sample_count,)
    np.testing.assert_array_equal(audio, np.full(sample_count, 0.25, dtype=np.float32))
