#!/usr/bin/env python3
"""Record the scripted bug reproduction as a SILENT screencast (~18 s).

The point of the silence: recorders like Windows Game Bar don't capture the
microphone by default, so real-world bug clips often have no narration.
talkthrough handles that — frames and on-screen text are indexed either way —
and this example proves it end to end.

Setup (any venv):

    pip install playwright
    playwright install chromium

Run from this directory:

    python record_repro.py

Writes ``checkout-coupon-bug.mp4`` (H.264, no audio track) next to itself.
Requires ``ffmpeg`` on PATH for the webm → mp4 remux (or set FFMPEG=/path).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = HERE / "checkout-demo.html"
OUT = HERE / "checkout-coupon-bug.mp4"
VIDEO_TMP = HERE / "_video"
SIZE = {"width": 1280, "height": 800}


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport=SIZE, record_video_dir=str(VIDEO_TMP), record_video_size=SIZE
        )
        page = context.new_page()
        page.goto(PAGE.as_uri())
        page.wait_for_timeout(2500)  # opening hold: cart + €100.00 total

        page.click("#coupon")
        page.type("#coupon", "SAVE20", delay=140)
        page.wait_for_timeout(700)
        page.click("#apply")  # €100.00 → €80.00, discount row appears
        page.wait_for_timeout(2800)

        page.click("#pay")  # "Processing…" → 402 → red banner + dev log
        page.wait_for_timeout(2200)
        page.hover("#banner")
        page.wait_for_timeout(2500)
        page.hover("#devlog")
        page.wait_for_timeout(3500)  # hold on the evidence: 402 amount_too_small

        video = page.video
        context.close()
        webm = video.path()
        browser.close()

    ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
    subprocess.run(
        [
            ffmpeg, "-y", "-i", str(webm),
            "-an",  # no audio track at all — the recording is silent
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-movflags", "+faststart",
            str(OUT),
        ],
        check=True,
    )
    shutil.rmtree(VIDEO_TMP, ignore_errors=True)
    print(OUT)


if __name__ == "__main__":
    main()
