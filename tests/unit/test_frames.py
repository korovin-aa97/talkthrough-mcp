"""showinfo pts parsing, frame naming, and the adaptive selection floor."""

from __future__ import annotations

from talkthrough_mcp.core.frames import frame_filename, frame_floor_s, parse_showinfo_pts_ms

# Shape captured from real ffmpeg stderr: showinfo lines interleaved with
# encoder chatter and deprecation warnings.
SHOWINFO_STDERR = """\
ffmpeg version 7.0 Copyright (c) 2000-2024 the FFmpeg developers
[vost#0:0 @ 0x600002e1c000] The -vsync option is deprecated. Use the -fps_mode option instead.
[Parsed_showinfo_2 @ 0x600001b34160] config in time_base: 1/12800, frame rate: 25/1
[Parsed_showinfo_2 @ 0x600001b34160] n:   0 pts:      0 pts_time:0       duration:    512 fmt:yuv420p cl:left sar:1/1 s:1280x720 i:P iskey:1 type:I checksum:C273BF31
[Parsed_showinfo_2 @ 0x600001b34160] n:   1 pts:  76877 pts_time:6.00602 duration:    512 fmt:yuv420p cl:left sar:1/1 s:1280x720 i:P iskey:0 type:P checksum:5A1B2C3D
[out#0/image2 @ 0x600002d1c0a0] video:154KiB audio:0KiB subtitle:0KiB
[Parsed_showinfo_2 @ 0x600001b34160] n:   2 pts: 153754 pts_time:12.0120 duration:    512 fmt:yuv420p cl:left sar:1/1 s:1280x720 i:P iskey:0 type:P checksum:9E8D7C6B
frame=    3 fps=0.0 q=4.0 Lsize=N/A time=00:00:12.01 bitrate=N/A speed= 150x
"""


def test_parse_showinfo_pts_ms_extracts_ordered_millis() -> None:
    assert parse_showinfo_pts_ms(SHOWINFO_STDERR) == [0, 6006, 12012]


def test_parse_showinfo_ignores_unrelated_lines() -> None:
    assert parse_showinfo_pts_ms("no frames here\nframe= 3 fps=0.0\n") == []


def test_parse_showinfo_handles_integer_pts_time() -> None:
    line = "[Parsed_showinfo_0 @ 0x1] n: 0 pts: 0 pts_time:7 duration: 1\n"
    assert parse_showinfo_pts_ms(line) == [7000]


def test_frame_filename_is_eight_digit_ms() -> None:
    assert frame_filename(0) == "t00000000.jpg"
    assert frame_filename(12345) == "t00012345.jpg"
    assert frame_filename(7_199_999) == "t07199999.jpg"


def test_frame_floor_stays_one_second_for_short_videos() -> None:
    """Every video that fits the budget at 1 fps keeps the historical floor —
    short-video extraction stays byte-identical to 0.1.x."""
    assert frame_floor_s(18.55, 600) == 1.0
    assert frame_floor_s(599.9, 600) == 1.0
    assert frame_floor_s(600.0, 600) == 1.0


def test_frame_floor_spreads_budget_over_long_videos() -> None:
    # 73-minute meeting at the default cap: one frame every ~7.3 s covers it all
    assert abs(frame_floor_s(4375.6, 600) - 7.2927) < 0.001
    assert frame_floor_s(7200.0, 600) == 12.0
    assert frame_floor_s(1200.0, 600) == 2.0


def test_frame_floor_degrades_to_one_second_without_duration() -> None:
    assert frame_floor_s(None, 600) == 1.0
    assert frame_floor_s(0.0, 600) == 1.0
    assert frame_floor_s(-5.0, 600) == 1.0
    assert frame_floor_s(100.0, 0) == 1.0
