"""Real FFmpeg operations with Unicode paths and legacy Windows pipe defaults."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from tests.integration.fixture_facts import DEMO_MP4

from talkthrough_mcp.core.audio import extract_wav
from talkthrough_mcp.core.errors import ToolFailureError
from talkthrough_mcp.core.ffmpeg import ffmpeg_path, run_tool
from talkthrough_mcp.core.frames import extract_exact_frame, extract_keyframes
from talkthrough_mcp.core.probe import probe_media


@pytest.fixture(scope="module", params=["mp4", "webm"])
def media_source(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    if request.param == "mp4":
        return Path(DEMO_MP4)
    media = tmp_path_factory.mktemp("unicode-source") / "recording.webm"
    run_tool(
        [
            ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(DEMO_MP4),
            "-vf",
            "scale=320:-2",
            "-r",
            "10",
            "-c:v",
            "libvpx",
            "-deadline",
            "realtime",
            "-b:v",
            "300k",
            "-c:a",
            "libopus",
            str(media),
        ],
        timeout=120,
    )
    return media


@pytest.mark.parametrize("code_page", ["cp1251", "cp1252"])
def test_native_media_operations_with_unicode_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code_page: str, media_source: Path
) -> None:
    # Includes the UTF-8 byte 0x98 (capital Cyrillic I), undefined in CP1251.
    directory = tmp_path / "\u0422\u0435\u043b\u0435\u043c\u043e\u0441\u0442"
    directory.mkdir()
    media = directory / ("\u0418\u0418 \u0432 NDBC \u2014 \u4f1a\u8bae" + media_source.suffix)
    shutil.copyfile(media_source, media)
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: code_page)

    info = probe_media(media)
    assert info.has_audio and info.has_video
    assert info.filename == media.name
    assert info.duration_s > 10

    wav_path = directory / "\u0437\u0432\u0443\u043a.wav"
    extract_wav(media, wav_path, timeout=120)
    assert wav_path.read_bytes()[:4] == b"RIFF"

    frames, _ = extract_keyframes(
        media, directory / "frames", max_frames=3, timeout=120, duration_s=info.duration_s
    )
    assert frames
    assert frames[-1].ms >= 6000
    assert all((directory / "frames" / frame.file).is_file() for frame in frames)

    exact_path = directory / "\u043a\u0430\u0434\u0440.jpg"
    extract_exact_frame(media, 6000, exact_path)
    assert exact_path.read_bytes()[:2] == b"\xff\xd8"


@pytest.mark.parametrize("code_page", ["cp1251", "cp1252"])
def test_exact_frame_failure_preserves_unicode_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code_page: str
) -> None:
    media = tmp_path / "\u0418\u0418.webm"
    media.touch()
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: code_page)

    with pytest.raises(ToolFailureError) as info:
        extract_exact_frame(media, 0, tmp_path / "frame.jpg")

    assert media.name in str(info.value)
