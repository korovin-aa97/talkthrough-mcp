"""Known facts about the committed fixtures — mirrored from make_fixtures.py.

Update BOTH files together when regenerating fixtures.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
DEMO_MP4 = FIXTURES_DIR / "talkthrough-demo.mp4"
MEETING_M4A = FIXTURES_DIR / "meeting-demo.m4a"

# -metadata creation_time=2026-07-10T10:00:00Z at mux time.
CREATION_TIME_ISO = "2026-07-10T10:00:00+00:00"

# Three 6 s scenes; -shortest trims the tail to the ~14.7 s narration.
SCENE_BOUNDARIES_MS = (0, 6000, 12000)
SCENE_TOLERANCE_MS = 1500

# The narration keywords that must survive whisper `tiny` (>=2 required).
SCRIPT_KEYWORDS = ("login", "dashboard", "error")

# Every scene title starts with this word (OCR target).
OCR_SCENE_WORD = "SCENE"

# Meeting narration keywords (>=1 required).
MEETING_KEYWORDS = ("action", "report", "team")
