#!/usr/bin/env python3
"""Render assets/social-preview.png (1280x640) for the GitHub repo card.

Run: uv run python scripts/make_social_preview.py
Upload manually: repo Settings → General → Social preview.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "assets" / "social-preview.png"

BG = (15, 20, 32)
FG = (235, 240, 248)
ACCENT = (96, 200, 255)
DIM = (150, 162, 180)
CHIP_BG = (28, 38, 56)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(
            "/System/Library/Fonts/Helvetica.ttc", size, index=1 if bold else 0
        )
    except OSError:
        return ImageFont.load_default(size=size)


def main() -> None:
    image = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(image)

    draw.text((80, 96), "talkthrough-mcp", font=font(78, bold=True), fill=FG)
    draw.text(
        (80, 208),
        "Record your screen. Talk.",
        font=font(44),
        fill=ACCENT,
    )
    draw.text(
        (80, 268),
        "Your AI agent files the bugs.",
        font=font(44),
        fill=ACCENT,
    )

    # arrows drawn manually — Helvetica via Pillow lacks the '→' glyph
    def arrow(x: int, y: int) -> None:
        draw.line((x, y, x + 30, y), fill=ACCENT, width=3)
        draw.polygon((x + 30, y - 7, x + 30, y + 7, x + 42, y), fill=ACCENT)

    left = "video/audio"
    middle = "transcript · keyframes · OCR · wall-clock"
    x = 80
    draw.text((x, 380), left, font=font(30), fill=FG)
    x += int(draw.textlength(left, font=font(30))) + 22
    arrow(x, 400)
    x += 64
    draw.text((x, 380), middle, font=font(30), fill=FG)
    x += int(draw.textlength(middle, font=font(30))) + 22
    arrow(x, 400)
    x += 64
    draw.text((x, 380), "findings", font=font(30), fill=FG)

    chips = ["MCP server", "local-first", "no cloud", "Whisper on CPU", "MIT"]
    x = 80
    for chip in chips:
        w = draw.textlength(chip, font=font(26))
        draw.rounded_rectangle((x, 470, x + w + 36, 522), radius=26, fill=CHIP_BG)
        draw.text((x + 18, 482), chip, font=font(26), fill=DIM)
        x += int(w) + 52

    draw.text((80, 566), "github.com/korovin-aa97/talkthrough-mcp", font=font(24), fill=DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(f"{OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
