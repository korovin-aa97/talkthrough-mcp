#!/usr/bin/env python3
"""Generate the committed test fixtures. Run ONCE on macOS; commit the output.

Outputs (committed, consumed as-is by CI — `say` is macOS-only):

- ``talkthrough-demo.mp4`` — three 6 s scenes (solid color + large title text
  rendered via Pillow and overlaid), narrated with the macOS ``say`` voice
  reading a fixed English script, muxed with a known ``creation_time`` tag.
- ``meeting-demo.m4a`` — a short say-only audio recording (no video stream).

Scene boundaries and the script keywords are mirrored in
``tests/integration/fixture_facts.py`` — update both together.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
BUILD_DIR = FIXTURES_DIR / "_build"

CREATION_TIME = "2026-07-10T10:00:00Z"
SCENE_SECONDS = 6
FRAME_SIZE = (1280, 720)
SAY_RATE = "160"

# Keywords must survive whisper `tiny`: plain words, clearly spoken.
DEMO_SCRIPT = (
    "This is the login page. The username field is empty and the submit button "
    "is disabled. Now I open the dashboard. There is an error message in the "
    "top right corner. It says something went wrong. Finally, this is the "
    "settings screen. Everything here looks fine."
)
MEETING_SCRIPT = (
    "Quick sync notes. First action item, send the weekly report to the team "
    "by Friday. Second action item, schedule a follow up call with the design "
    "team next week. We decided to postpone the pricing change until September."
)

SCENES = [
    {"color": "0x1E3A5F", "title": "SCENE LOGIN PAGE", "subtitle": ""},
    {"color": "0x6B1D1D", "title": "SCENE DASHBOARD ERROR", "subtitle": "Something went wrong"},
    {"color": "0x1F5F2E", "title": "SCENE SETTINGS", "subtitle": ""},
]


def _ffmpeg() -> str:
    sys.path.insert(0, str(FIXTURES_DIR.parents[1] / "src"))
    from talkthrough_mcp.core.ffmpeg import ffmpeg_path

    return ffmpeg_path()


def _say(text: str, out_aiff: Path) -> None:
    cmd = ["/usr/bin/say", "-r", SAY_RATE, "-o", str(out_aiff), text]
    try:
        subprocess.run([*cmd[:3], "-v", "Samantha", *cmd[3:]], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(cmd, check=True, capture_output=True)  # default voice fallback


def _title_png(title: str, subtitle: str, out_png: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        try:
            return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
        except OSError:
            return ImageFont.load_default(size=size)

    image = Image.new("RGBA", FRAME_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    main_font, sub_font = font(96), font(56)
    width, height = FRAME_SIZE

    box = draw.textbbox((0, 0), title, font=main_font)
    x = (width - (box[2] - box[0])) / 2
    y = (height - (box[3] - box[1])) / 2 - (60 if subtitle else 0)
    draw.text((x, y), title, font=main_font, fill="white", stroke_width=4, stroke_fill="black")
    if subtitle:
        sbox = draw.textbbox((0, 0), subtitle, font=sub_font)
        sx = (width - (sbox[2] - sbox[0])) / 2
        draw.text(
            (sx, y + 160),
            subtitle,
            font=sub_font,
            fill="white",
            stroke_width=3,
            stroke_fill="black",
        )
    image.save(out_png)


def build_demo_mp4(ffmpeg: str) -> Path:
    out = FIXTURES_DIR / "talkthrough-demo.mp4"
    speech = BUILD_DIR / "demo-speech.aiff"
    _say(DEMO_SCRIPT, speech)

    overlays: list[Path] = []
    for index, scene in enumerate(SCENES):
        png = BUILD_DIR / f"title-{index}.png"
        _title_png(scene["title"], scene["subtitle"], png)
        overlays.append(png)

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(speech)]
    for scene in SCENES:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"color=c={scene['color']}:s={FRAME_SIZE[0]}x{FRAME_SIZE[1]}:r=25:d={SCENE_SECONDS}",
        ]
    for png in overlays:
        cmd += ["-i", str(png)]

    scene_count = len(SCENES)
    parts = []
    for index in range(scene_count):
        color_input = 1 + index
        png_input = 1 + scene_count + index
        parts.append(f"[{color_input}:v][{png_input}:v]overlay=(W-w)/2:(H-h)/2[v{index}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(scene_count))
    parts.append(f"{concat_inputs}concat=n={scene_count}:v=1:a=0[v]")
    filter_complex = ";".join(parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        "-metadata",
        f"creation_time={CREATION_TIME}",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def build_meeting_m4a(ffmpeg: str) -> Path:
    out = FIXTURES_DIR / "meeting-demo.m4a"
    speech = BUILD_DIR / "meeting-speech.aiff"
    _say(MEETING_SCRIPT, speech)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(speech),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-metadata",
            f"creation_time={CREATION_TIME}",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("fixture generation uses macOS `say`; run it on macOS once")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg()
    demo = build_demo_mp4(ffmpeg)
    meeting = build_meeting_m4a(ffmpeg)
    for path in (demo, meeting):
        print(f"{path.name}: {path.stat().st_size / 1_000_000:.2f} MB")


if __name__ == "__main__":
    main()
