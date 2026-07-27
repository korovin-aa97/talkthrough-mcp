#!/usr/bin/env python3
"""Render assets/social-preview.png (2560x1280 — 2x the GitHub card size) .

Run: uv run python scripts/make_social_preview.py
Upload manually: repo Settings → General → Social preview.

v2 (0.2.4): carries the README hero slogan and the /talkthrough:bug
command, matching the demo-GIF end card.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "assets" / "social-preview.png"

S = 2  # render scale (GitHub card is 1280x640)
BG = (15, 20, 32)
FG = (235, 240, 248)
ACCENT = (96, 200, 255)
DIM = (150, 162, 180)
CHIP_BG = (28, 38, 56)
CMD_BG = (22, 30, 46)
CMD_BORDER = (52, 66, 92)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(
            "/System/Library/Fonts/Helvetica.ttc", size * S, index=1 if bold else 0
        )
    except OSError:
        return ImageFont.load_default(size=size * S)


def mono(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size * S)
    except OSError:
        return ImageFont.load_default(size=size * S)


def main() -> None:
    image = Image.new("RGB", (1280 * S, 640 * S), BG)
    draw = ImageDraw.Draw(image)

    def text(x: int, y: int, s: str, f: ImageFont.FreeTypeFont, fill: tuple) -> int:
        draw.text((x * S, y * S), s, font=f, fill=fill)
        return int(draw.textlength(s, font=f) / S)

    text(80, 56, "talkthrough-mcp", font(26, bold=True), DIM)

    text(80, 118, "Don't write a bug report.", font(76, bold=True), FG)
    text(80, 218, "Record it.", font(76, bold=True), ACCENT)

    # command chip, like the demo-GIF terminal line
    cmd_y = 356
    pad = 26
    parts = [("> ", DIM), ("/talkthrough:bug", ACCENT), (" recording.mov", FG)]
    total = sum(draw.textlength(s, font=mono(30)) for s, _ in parts) / S
    draw.rounded_rectangle(
        (80 * S, cmd_y * S, (80 + pad * 2 + total) * S, (cmd_y + 62) * S),
        radius=14 * S,
        fill=CMD_BG,
        outline=CMD_BORDER,
        width=S,
    )
    x = 80 + pad
    for s, color in parts:
        x += text(x, cmd_y + 14, s, mono(30), color)

    text(
        80,
        468,
        "Local MCP server: transcript · exact frames · OCR · wall-clock evidence "
        "for coding agents",
        font(28),
        FG,
    )

    chips = ["Claude Code", "Codex", "local-first", "no cloud", "MIT"]
    x = 80
    for chip in chips:
        w = draw.textlength(chip, font=font(26)) / S
        draw.rounded_rectangle(
            (x * S, 524 * S, (x + w + 36) * S, 576 * S), radius=26 * S, fill=CHIP_BG
        )
        text(x + 18, 536, chip, font(26), DIM)
        x += int(w) + 52

    text(80, 596, "github.com/korovin-aa97/talkthrough-mcp", font(24), DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(f"{OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
