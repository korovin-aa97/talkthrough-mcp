#!/usr/bin/env python3
"""Render benchmarks/scores-by-task-{light,dark}.svg from the frozen v0.2.0
battery data (docs/MODEL-NOTES.md, Matrix 1).

Dot plot: one row per task (sorted by pooled mean), one dot per runner
config, colored by model family. Palette validated for CVD + contrast in
both modes (2 categorical slots); identity is never color-alone — the
legend names the configs and the tables in benchmarks/README.md carry the
exact numbers.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "benchmarks"

# (task label, [haiku, sonnet, opus, gpt-5.5 med, gpt-5.5 high, gpt-5.4-mini low])
TASKS: list[tuple[str, list[float]]] = [
    ("Point lookup: who said X, and when", [2.0, 2.0, 2.0, 2.0, 2.0, 2.0]),
    ("Bug triage → evidence-backed findings JSON", [1.0, 2.0, 2.0, 2.0, 2.0, 2.0]),
    ("Find the key slide, return the screenshot", [2.0, 1.0, 1.5, 2.0, 2.0, 2.0]),
    ("Bug screencast stays cheap (no pointless diarization)", [1.0, 2.0, 2.0, 2.0, 2.0, 1.0]),
    ("Ingest with who-said-what intent (parameter choice)", [2.0, 1.3, 1.7, 1.7, 1.0, 1.7]),
    ("Map speaker labels to real names, with evidence", [1.3, 1.7, 0.7, 1.7, 2.0, 1.0]),
    ("Naive “analyze this meeting” (zero hints)", [1.0, 0.5, 1.5, 1.0, 2.0, 1.5]),
    ("Meeting minutes with owners", [0.0, 0.5, 2.0, 0.5, 2.0, 0.5]),
]
FAMILY = ["claude", "claude", "claude", "codex", "codex", "codex"]

MODES = {
    "light": {
        "surface": "#fcfcfb",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "baseline": "#c3c2b7",
        "claude": "#2a78d6",
        "codex": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "baseline": "#383835",
        "claude": "#3987e5",
        "codex": "#d95926",
    },
}

W, H = 960, 620
PLOT_X0, PLOT_X1 = 400, 900
ROW_H = 50
PLOT_Y0 = 118
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def x_for(score: float) -> float:
    return PLOT_X0 + (PLOT_X1 - PLOT_X0) * score / 2.0


def render(mode: str) -> str:
    c = MODES[mode]
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="{FONT}">'
    )
    parts.append(f'<rect width="{W}" height="{H}" fill="{c["surface"]}" rx="8"/>')
    parts.append(
        f'<text x="32" y="40" font-size="19" font-weight="600" fill="{c["primary"]}">'
        "Which jobs need which agent tier?</text>"
    )
    parts.append(
        f'<text x="32" y="62" font-size="13" fill="{c["secondary"]}">'
        "Mean judge score per task · 6 runner configs · 132 judged runs on real recordings "
        "(30 s – 73 min, RU/EN) · v0.2.0 battery, July 2026</text>"
    )
    # legend (2 series)
    lx = 32
    ly = 88
    parts.append(f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{c["claude"]}"/>')
    parts.append(
        f'<text x="{lx + 16}" y="{ly}" font-size="12.5" fill="{c["secondary"]}">'
        "Claude — haiku · sonnet · opus (defaults)</text>"
    )
    lx2 = 330
    parts.append(f'<circle cx="{lx2 + 5}" cy="{ly - 4}" r="5" fill="{c["codex"]}"/>')
    parts.append(
        f'<text x="{lx2 + 16}" y="{ly}" font-size="12.5" fill="{c["secondary"]}">'
        "Codex — gpt-5.5 medium · gpt-5.5 high · gpt-5.4-mini low</text>"
    )

    plot_h = ROW_H * len(TASKS)
    # gridlines + tick labels
    for score in (0.0, 0.5, 1.0, 1.5, 2.0):
        gx = x_for(score)
        parts.append(
            f'<line x1="{gx}" y1="{PLOT_Y0}" x2="{gx}" y2="{PLOT_Y0 + plot_h}" '
            f'stroke="{c["grid"]}" stroke-width="1"/>'
        )
        label = f"{score:g}"
        parts.append(
            f'<text x="{gx}" y="{PLOT_Y0 + plot_h + 20}" font-size="12" '
            f'fill="{c["muted"]}" text-anchor="middle">{label}</text>'
        )
    parts.append(
        f'<text x="{(PLOT_X0 + PLOT_X1) / 2}" y="{PLOT_Y0 + plot_h + 44}" font-size="12" '
        f'fill="{c["muted"]}" text-anchor="middle">mean judge score — '
        "0 failed/fabricated · 1 partial · 2 correct and fully evidenced</text>"
    )

    for row, (label, scores) in enumerate(TASKS):
        cy = PLOT_Y0 + ROW_H * row + ROW_H / 2
        parts.append(
            f'<text x="{PLOT_X0 - 16}" y="{cy + 4}" font-size="13" '
            f'fill="{c["primary"]}" text-anchor="end">{label}</text>'
        )
        # two fixed lanes per row (Claude above, Codex below) keep rows visually
        # separate; exact overlaps within a lane dodge horizontally (coin stack)
        for family, lane_dy in (("claude", -8.0), ("codex", 8.0)):
            lane = [
                score for idx, score in enumerate(scores) if FAMILY[idx] == family
            ]
            by_score: dict[float, int] = {}
            for score in lane:
                by_score[score] = by_score.get(score, 0) + 1
            for score, n in by_score.items():
                for pos in range(n):
                    dx = (pos - (n - 1) / 2) * 7
                    parts.append(
                        f'<circle cx="{x_for(score) + dx:.1f}" cy="{cy + lane_dy:.1f}" '
                        f'r="5" fill="{c[family]}" stroke="{c["surface"]}" '
                        'stroke-width="2"/>'
                    )

    # selective direct labels (top row + bottom row tell the story)
    cy_first = PLOT_Y0 + ROW_H / 2
    parts.append(
        f'<text x="{x_for(2.0) - 30}" y="{cy_first + 4}" font-size="11.5" '
        f'fill="{c["muted"]}" text-anchor="end">every tier tested</text>'
    )
    cy_last = PLOT_Y0 + ROW_H * 7 + ROW_H / 2
    parts.append(
        f'<text x="{x_for(2.0) - 30}" y="{cy_last + 4}" font-size="11.5" '
        f'fill="{c["muted"]}" text-anchor="end">only opus and gpt-5.5 high</text>'
    )
    parts.append(
        f'<text x="32" y="{H - 16}" font-size="11.5" fill="{c["muted"]}">'
        "n per cell = 1–3 runs — read as tiers, not a leaderboard · exact numbers and "
        "per-release regression batteries: docs/MODEL-NOTES.md</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for mode in MODES:
        path = OUT_DIR / f"scores-by-task-{mode}.svg"
        path.write_text(render(mode), encoding="utf-8")
        print(path.relative_to(REPO))


if __name__ == "__main__":
    main()
