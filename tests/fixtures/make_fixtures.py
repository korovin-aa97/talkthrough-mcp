#!/usr/bin/env python3
"""Generate the committed test fixtures. Run ONCE on macOS; commit the output.

Outputs (committed, consumed as-is by CI — `say` is macOS-only):

- ``talkthrough-demo.mp4`` — three 6 s scenes (solid color + large title text
  rendered via Pillow and overlaid), narrated with the macOS ``say`` voice
  reading a fixed English script, muxed with a known ``creation_time`` tag.
- ``meeting-demo.m4a`` — a short say-only audio recording (no video stream).
- ``multilang-ru-demo.m4a`` — a short Russian narration (Milena voice) for
  the language-detection test.
- ``meeting-two-voices.m4a`` — five alternating turns spoken by two clearly
  different voices (Samantha en_US / Daniel en_GB), every turn well above
  the sub-second-backchannel weakness of diarization models; the builder
  prints the measured turn boundaries to paste into ``fixture_facts.py``.
- ``multilang-ja-demo.mp4`` — one scene with a katakana-heavy heading,
  narrated in Japanese (Kyoko voice): the auto-OCR-pack fixture. Helvetica
  carries no CJK glyphs, so the heading renders with the first installed
  font from ``CJK_FONT_CANDIDATES`` (Hiragino Sans first).

Scene boundaries and the script keywords are mirrored in
``tests/integration/fixture_facts.py`` — update both together.

Rebuild selectively: ``python make_fixtures.py ru`` (targets: demo, meeting,
ru, two-voice, ja; default all) — so adding a fixture never rewrites the
committed bytes of the others.
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
RU_SCRIPT = (
    "Это тестовая запись на русском языке. Кнопка отправки не работает, это "
    "блокирует весь сценарий. Ещё я хочу фильтр по дате в списке заказов. "
    "Волшебное слово — карбюратор."
)

# Two-voice meeting: labels are assigned by first appearance, so Samantha
# (turn 1) is S1 and Daniel is S2. Turns are 5+ s each — far above the
# min_duration_on smoothing and the <0.8 s backchannel weakness.
TWO_VOICE_TURNS = [
    (
        "Samantha",
        "Welcome everyone to the weekly planning meeting. Today we need to "
        "decide on the release date for the new version.",
    ),
    (
        "Daniel",
        "Thanks for having me. I reviewed the deployment checklist yesterday "
        "and found two open issues we should discuss.",
    ),
    (
        "Samantha",
        "That sounds important. Please walk us through the first issue and "
        "tell us how long the fix would take.",
    ),
    (
        "Daniel",
        "The first issue is about the database migration script. It fails on "
        "large tables and needs at least three more days of work.",
    ),
    (
        "Samantha",
        "Understood. Then let us move the release to next Thursday and review "
        "the progress again on Monday morning.",
    ),
]

SCENES = [
    {"color": "0x1E3A5F", "title": "SCENE LOGIN PAGE", "subtitle": ""},
    {"color": "0x6B1D1D", "title": "SCENE DASHBOARD ERROR", "subtitle": "Something went wrong"},
    {"color": "0x1F5F2E", "title": "SCENE SETTINGS", "subtitle": ""},
]

# Japanese fixture: whisper tiny detects the language reliably on clean
# speech; the heading is katakana-heavy ON PURPOSE — the default
# Latin+Chinese recognition model cannot read kana, so an OCR hit on it
# proves the japan pack was actually engaged.
JA_SCRIPT = (
    "これはテスト録画です。ログインボタンが動作しません。"
    "エラーメッセージが表示されています。設定画面を確認してください。"
)
JA_SCENE = {"color": "0x3B1E5F", "title": "ログイン画面", "subtitle": "エラーが発生しました"}

# Helvetica.ttc has no CJK glyphs — first installed candidate wins.
CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",  # Hiragino Sans W3
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _ffmpeg() -> str:
    sys.path.insert(0, str(FIXTURES_DIR.parents[1] / "src"))
    from talkthrough_mcp.core.ffmpeg import ffmpeg_path

    return ffmpeg_path()


def _ffprobe() -> str:
    sys.path.insert(0, str(FIXTURES_DIR.parents[1] / "src"))
    from talkthrough_mcp.core.ffmpeg import ffprobe_path

    return ffprobe_path()


def _duration_s(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _say(text: str, out_aiff: Path, *, voice: str = "Samantha") -> None:
    cmd = ["/usr/bin/say", "-r", SAY_RATE, "-o", str(out_aiff), text]
    try:
        subprocess.run([*cmd[:3], "-v", voice, *cmd[3:]], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(cmd, check=True, capture_output=True)  # default voice fallback


def _cjk_font_path() -> str:
    for candidate in CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit(
        "no CJK-capable font found — install Hiragino Sans or Arial Unicode "
        f"(looked for: {', '.join(CJK_FONT_CANDIDATES)})"
    )


def _title_png(
    title: str,
    subtitle: str,
    out_png: Path,
    *,
    font_path: str = "/System/Library/Fonts/Helvetica.ttc",
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        try:
            return ImageFont.truetype(font_path, size)
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


def build_ru_m4a(ffmpeg: str) -> Path:
    out = FIXTURES_DIR / "multilang-ru-demo.m4a"
    speech = BUILD_DIR / "ru-speech.aiff"
    _say(RU_SCRIPT, speech, voice="Milena")
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
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def build_two_voice_m4a(ffmpeg: str) -> Path:
    """Alternating two-voice meeting + printed turn boundaries for fixture_facts."""
    out = FIXTURES_DIR / "meeting-two-voices.m4a"
    ffprobe = _ffprobe()

    clips: list[Path] = []
    durations_ms: list[int] = []
    for index, (voice, text) in enumerate(TWO_VOICE_TURNS):
        clip = BUILD_DIR / f"two-voice-{index}.aiff"
        # No default-voice fallback here on purpose: a silently substituted
        # voice would produce a single-speaker file that still LOOKS valid.
        subprocess.run(
            ["/usr/bin/say", "-r", SAY_RATE, "-v", voice, "-o", str(clip), text],
            check=True,
            capture_output=True,
        )
        clips.append(clip)
        durations_ms.append(round(_duration_s(ffprobe, clip) * 1000))

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    streams = "".join(f"[{i}:a]" for i in range(len(clips)))
    cmd += [
        "-filter_complex",
        f"{streams}concat=n={len(clips)}:v=0:a=1[a]",
        "-map",
        "[a]",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-metadata",
        f"creation_time={CREATION_TIME}",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    labels = {"Samantha": "S1", "Daniel": "S2"}
    cursor = 0
    print("paste into fixture_facts.py TWO_VOICE_TURNS_MS:")
    for (voice, _), duration_ms in zip(TWO_VOICE_TURNS, durations_ms, strict=True):
        print(f"    ({cursor}, {cursor + duration_ms}, {labels[voice]!r}),")
        cursor += duration_ms
    return out


def build_ja_mp4(ffmpeg: str) -> Path:
    """One-scene Japanese screencast: Kyoko narration + kana/kanji heading."""
    out = FIXTURES_DIR / "multilang-ja-demo.mp4"
    speech = BUILD_DIR / "ja-speech.aiff"
    # No default-voice fallback on purpose: an English voice cannot read the
    # script, and a silently substituted voice would break language detection.
    subprocess.run(
        ["/usr/bin/say", "-r", SAY_RATE, "-v", "Kyoko", "-o", str(speech), JA_SCRIPT],
        check=True,
        capture_output=True,
    )
    png = BUILD_DIR / "ja-title.png"
    _title_png(JA_SCENE["title"], JA_SCENE["subtitle"], png, font_path=_cjk_font_path())
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(speech),
        "-f",
        "lavfi",
        "-i",
        f"color=c={JA_SCENE['color']}:s={FRAME_SIZE[0]}x{FRAME_SIZE[1]}:r=25:d=60",
        "-i",
        str(png),
        "-filter_complex",
        "[1:v][2:v]overlay=(W-w)/2:(H-h)/2[v]",
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


BUILDERS = {
    "demo": build_demo_mp4,
    "meeting": build_meeting_m4a,
    "ru": build_ru_m4a,
    "two-voice": build_two_voice_m4a,
    "ja": build_ja_mp4,
}


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("fixture generation uses macOS `say`; run it on macOS once")
    targets = sys.argv[1:] or list(BUILDERS)
    unknown = set(targets) - set(BUILDERS)
    if unknown:
        raise SystemExit(f"unknown targets {sorted(unknown)}; choose from {sorted(BUILDERS)}")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg()
    for target in targets:
        path = BUILDERS[target](ffmpeg)
        print(f"{path.name}: {path.stat().st_size / 1_000_000:.2f} MB")


if __name__ == "__main__":
    main()
