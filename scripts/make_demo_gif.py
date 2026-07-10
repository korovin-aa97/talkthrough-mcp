#!/usr/bin/env python3
"""Render assets/demo.gif — a terminal-style replay of a real session.

The frames are drawn with Pillow (no vhs/asciinema dependency) and assembled
with the same ffmpeg the package already resolves. All output text is a real
capture from processing tests/fixtures/talkthrough-demo.mp4 (whisper small),
with only the filename cosmetically renamed.

Run: uv run python scripts/make_demo_gif.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "assets" / "demo.gif"

W, H = 840, 520
BG = (13, 17, 27)
FG = (214, 222, 235)
DIM = (130, 142, 160)
GREEN = (126, 211, 129)
CYAN = (105, 195, 255)
YELLOW = (229, 192, 123)
BAR = (24, 32, 48)

CMD = "talkthrough-mcp process ~/Desktop/bug-repro.mov"

PROGRESS = [
    "[  5.0%] probing media",
    "[ 15.0%] transcribing (local whisper)",
    "[ 72.0%] extracting keyframes",
    "[ 86.0%] reading text on frames (OCR)",
    "[100.0%] done",
]

SUMMARY = [
    ("job_id     : 4d0695c8ab1e38ac", FG),
    ("media      : bug-repro.mov [video] 14.7s", FG),
    ("wall clock : 2026-07-10T10:00:00+00:00 (source=metadata, medium)", CYAN),
    ("transcript : 7 segments (language=en, model=small)", FG),
    ("frames     : 3 unique / 15 total", FG),
    ("ocr        : enabled=True frames_with_text=3", FG),
    ("preview    :", FG),
    ("  [      0 ms] This is the login page.", DIM),
    ("  [   4960 ms] Now I open the dashboard.", DIM),
    ("  [   6680 ms] There is an error message in the top right corner.", DIM),
]

AGENT = [
    ("# agent side — lazy retrieval by job_id:", DIM),
    ("search(job_id, \"error\")", YELLOW),
    ("  hit: t_ms=6680  t_wall=2026-07-10T10:00:06+00:00", FG),
    ("       text=\"There is an error message in the top right corner.\"", FG),
    ("       nearest_frame_ms=6000  → get_moment(job_id, 4680, 8680)", CYAN),
    ("  hit: t_ms=6000  source=ocr  text=\"SCENE DASHBOARD ERROR ...\"", FG),
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


F = font(15)
LINE_H = 24
TOP = 56


def new_frame() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, W, 36), radius=0, fill=BAR)
    for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        draw.ellipse((16 + i * 24, 12, 28 + i * 24, 24), fill=color)
    draw.text((W // 2 - 60, 10), "talkthrough", font=font(13), fill=DIM)
    return image, draw


def draw_lines(draw: ImageDraw.ImageDraw, lines: list[tuple[str, tuple[int, int, int]]]) -> None:
    y = TOP
    for text, color in lines:
        draw.text((20, y), text, font=F, fill=color)
        y += LINE_H


def prompt(cmd_part: str, cursor: bool = True) -> tuple[str, tuple[int, int, int]]:
    return (f"$ {cmd_part}{'▌' if cursor else ''}", GREEN)


def main() -> None:
    frames: list[tuple[Image.Image, int]] = []  # (image, duration_cs)

    # typing the command
    for cut in (18, 34, len(CMD)):
        image, draw = new_frame()
        draw_lines(draw, [prompt(CMD[:cut])])
        frames.append((image, 45))

    # progress accumulates
    for upto in range(1, len(PROGRESS) + 1):
        image, draw = new_frame()
        lines = [prompt(CMD, cursor=False)]
        lines += [(line, DIM if i < upto - 1 else YELLOW) for i, line in enumerate(PROGRESS[:upto])]
        frames.append((image, 60))
        draw_lines(draw, lines)

    # summary
    image, draw = new_frame()
    draw_lines(draw, [prompt(CMD, cursor=False), *SUMMARY])
    frames.append((image, 330))

    # agent-side view
    image, draw = new_frame()
    draw_lines(draw, [(line, color) for line, color in AGENT])
    frames.append((image, 380))

    # closing card
    image, draw = new_frame()
    draw_lines(
        draw,
        [
            ("Record your screen. Talk.", CYAN),
            ("Your agent files the bugs.", CYAN),
            ("", FG),
            ("uvx talkthrough-mcp   ·   local-first   ·   MIT", FG),
        ],
    )
    frames.append((image, 300))

    sys.path.insert(0, str(REPO / "src"))
    from talkthrough_mcp.core.ffmpeg import ffmpeg_path

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        concat = Path(tmp) / "concat.txt"
        entries = []
        for index, (image, duration_cs) in enumerate(frames):
            name = f"f{index:03d}.png"
            image.save(Path(tmp) / name)
            entries.append(f"file '{name}'\nduration {duration_cs / 100:.2f}")
        entries.append(f"file 'f{len(frames) - 1:03d}.png'")  # concat quirk: repeat last
        concat.write_text("\n".join(entries), encoding="utf-8")
        subprocess.run(
            [
                ffmpeg_path(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-vf",
                "split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=none",
                "-loop",
                "0",
                str(OUT),
            ],
            check=True,
        )
    print(f"{OUT} ({OUT.stat().st_size / 1024:.0f} KiB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
