from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.extract_release_notes import ReleaseNotesError, extract_release_notes

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "extract_release_notes.py"

CHANGELOG = """\
# Changelog

## [0.3.2] — 2026-09-03

### Fixed

- Kept the stored job safe.

## [0.3.1] — 2026-09-02

- Previous release.
"""


def test_extracts_exact_nonempty_section_without_next_release() -> None:
    notes = extract_release_notes(CHANGELOG, "0.3.2", tag="v0.3.2")
    assert notes.startswith("## [0.3.2] — 2026-09-03\n")
    assert "Kept the stored job safe" in notes
    assert "0.3.1" not in notes
    assert notes.endswith("\n")


@pytest.mark.parametrize(
    ("changelog", "error"),
    [
        ("# Changelog\n", "missing"),
        (CHANGELOG + "\n## [0.3.2]\n\n- Again.\n", "duplicate"),
        ("## [0.3.2] — soon\n\n## [0.3.1]\n\n- Old.\n", "empty"),
    ],
)
def test_rejects_missing_duplicate_or_empty_section(changelog: str, error: str) -> None:
    with pytest.raises(ReleaseNotesError, match=error):
        extract_release_notes(changelog, "0.3.2")


@pytest.mark.parametrize(
    ("version", "tag", "error"),
    [
        ("0.3.2", "v0.3.1", "mismatch"),
        ("0.3.2", "0.3.2", "must start"),
        ("release", "vrelease", "invalid version"),
    ],
)
def test_rejects_invalid_or_mismatched_version_tag(
    version: str, tag: str, error: str
) -> None:
    with pytest.raises(ReleaseNotesError, match=error):
        extract_release_notes(CHANGELOG, version, tag=tag)


def test_cli_writes_output_file(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    output = tmp_path / "release-notes.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--version",
            "0.3.2",
            "--tag",
            "v0.3.2",
            "--changelog",
            str(changelog),
            "--output",
            str(output),
        ],
        check=True,
    )
    assert output.read_text(encoding="utf-8") == extract_release_notes(
        CHANGELOG, "0.3.2", tag="v0.3.2"
    )


def test_release_workflow_checks_project_version_before_publish() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert 'PROJECT_VERSION="$(uv version --short)"' in workflow
    assert '--version "$PROJECT_VERSION"' in workflow
    assert '--tag "$GITHUB_REF_NAME"' in workflow
    assert "--notes-file release-notes.md" in workflow
    assert "--generate-notes" not in workflow
