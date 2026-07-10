"""Keyframe extraction: ONE decode pass, scene-change OR 1 fps floor.

The select expression keeps a frame when any of these holds:
- it is the first frame (``isnan(prev_selected_t)``),
- the scene-change score exceeds the threshold,
- at least 1 s passed since the last selected frame (floor for static scenes).

Frames are scaled to <=1568 px wide in the SAME filter chain (vision-model
sweet spot; the source video is never re-read for normal frame serving) and
named ``t<video-ms, 8 digits>.jpg`` so a transcript timestamp maps straight
to its screenshot. Millisecond positions come from ffmpeg ``showinfo`` logs.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ToolFailureError, ValidationError
from .ffmpeg import ffmpeg_path

SHOWINFO_PTS_RE = re.compile(r"Parsed_showinfo.*?\bpts_time:\s*(?P<pts>[0-9]+(?:\.[0-9]+)?)")

DEFAULT_SCENE_THRESHOLD = 0.10
MAX_FRAME_WIDTH = 1568


@dataclass
class Frame:
    ms: int
    file: str  # filename inside the job's frames/ dir
    duplicate_of: int | None = None  # ms of the unique frame this duplicates
    ocr_text: str | None = field(default=None)

    @property
    def is_unique(self) -> bool:
        return self.duplicate_of is None


def parse_showinfo_pts_ms(stderr: str) -> list[int]:
    """Selected-frame timestamps (ms) in output order from ffmpeg showinfo logs."""
    return [int(float(match.group("pts")) * 1000) for match in SHOWINFO_PTS_RE.finditer(stderr)]


def frame_filename(ms: int) -> str:
    return f"t{ms:08d}.jpg"


def extract_keyframes(
    media: Path,
    frames_dir: Path,
    *,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    max_frames: int,
    timeout: int,
) -> tuple[list[Frame], bool]:
    """One-pass scene-change + 1 fps floor extraction, renamed to video-ms."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    select_expr = f"isnan(prev_selected_t)+gt(scene\\,{scene_threshold})+gte(t-prev_selected_t\\,1)"
    vf = f"select='{select_expr}',scale='min({MAX_FRAME_WIDTH},iw)':-2,showinfo"
    try:
        proc = subprocess.run(
            [
                ffmpeg_path(),
                "-y",
                "-hide_banner",
                "-i",
                str(media),
                "-vf",
                vf,
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(max_frames),
                "-q:v",
                "4",
                str(frames_dir / "raw-%06d.jpg"),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or "").strip().splitlines()[-8:]
        raise ToolFailureError(
            f"frame extraction failed (rc={exc.returncode}): " + " | ".join(stderr_tail)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolFailureError(f"frame extraction timed out after {timeout}s") from exc

    pts_ms = parse_showinfo_pts_ms(proc.stderr)
    raw_files = sorted(frames_dir.glob("raw-*.jpg"))
    frames: list[Frame] = []
    seen_ms: set[int] = set()
    for index, raw in enumerate(raw_files):
        ms = pts_ms[index] if index < len(pts_ms) else index * 1000
        while ms in seen_ms:
            ms += 1
        seen_ms.add(ms)
        final_name = frame_filename(ms)
        raw.rename(frames_dir / final_name)
        frames.append(Frame(ms=ms, file=final_name))
    frames.sort(key=lambda frame: frame.ms)
    cap_hit = len(raw_files) >= max_frames
    return frames, cap_hit


def extract_exact_frame(
    media: Path,
    at_ms: int,
    out_path: Path,
    *,
    crop: tuple[int, int, int, int] | None = None,
    timeout: int = 120,
) -> None:
    """Re-extract ONE full-resolution frame at an exact timestamp from the source.

    ``crop`` is ``(x, y, w, h)`` in source pixels. Used when the selected
    keyframes miss the exact moment; requires the source file to still exist.
    """
    if not media.is_file():
        raise ValidationError(
            f"source video no longer exists at its recorded path: {media} — "
            "keyframes captured at processing time are still available via get_frames"
        )
    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{at_ms / 1000:.3f}",
        "-i",
        str(media),
        "-frames:v",
        "1",
    ]
    if crop is not None:
        x, y, w, h = crop
        cmd += ["-vf", f"crop={w}:{h}:{x}:{y}"]
    cmd += ["-q:v", "2", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or "").strip().splitlines()[-5:]
        raise ToolFailureError(
            f"exact-frame extraction failed (rc={exc.returncode}): " + " | ".join(stderr_tail)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolFailureError(f"exact-frame extraction timed out after {timeout}s") from exc
    if not out_path.is_file():
        raise ToolFailureError(
            f"no frame produced at {at_ms} ms — the timestamp may be past the end of the video"
        )
