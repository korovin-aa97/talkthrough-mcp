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

# Russian narration (Milena voice) — language-detection fixture. Keyword
# asserts are deliberately absent: whisper `tiny` is weak at Russian
# TRANSCRIPTION, but language DETECTION on clean speech is reliable.
RU_M4A = FIXTURES_DIR / "multilang-ru-demo.m4a"
RU_LANGUAGE = "ru"

# Japanese narrated screencast (Kyoko voice) — the auto-OCR-pack fixture.
# The on-screen heading is katakana-heavy on purpose: the default
# Latin+Chinese recognition model cannot read kana, so an OCR hit on the
# word below proves the japan pack was auto-selected from the detected
# speech language (v0.2.1).
JA_MP4 = FIXTURES_DIR / "multilang-ja-demo.mp4"
JA_LANGUAGE = "ja"
JA_OCR_TITLE_WORD = "ログイン"  # katakana half of the "ログイン画面" heading

# Two-voice meeting: Samantha speaks first (=> S1 by first appearance),
# Daniel is S2. Boundaries were measured from the per-turn say clips at
# generation time; the AAC encode shifts the real edges by a few tens of ms,
# so tests must attribute by overlap majority, never assert exact edges.
TWO_VOICE_M4A = FIXTURES_DIR / "meeting-two-voices.m4a"
TWO_VOICE_NUM_SPEAKERS = 2
TWO_VOICE_TURNS_MS = (
    (0, 5621, "S1"),
    (5621, 11999, "S2"),
    (11999, 17071, "S1"),
    (17071, 24290, "S2"),
    (24290, 29754, "S1"),
)
# Narration keywords that must survive whisper `tiny` (>=1 required).
TWO_VOICE_KEYWORDS = ("release", "database", "issue")
