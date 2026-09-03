#!/usr/bin/env python3
"""Extract one exact Keep-a-Changelog release section for publishing."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


class ReleaseNotesError(ValueError):
    """The requested changelog section is not safe to publish."""


def normalized_version(value: str, *, field: str) -> str:
    version = value.removeprefix("v")
    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseNotesError(f"invalid {field}: {value!r}")
    return version


def extract_release_notes(changelog: str, version: str, *, tag: str | None = None) -> str:
    """Return exactly one non-empty ``## [version]`` section."""
    requested = normalized_version(version, field="version")
    if tag is not None:
        tagged = normalized_version(tag, field="tag")
        if not tag.startswith("v"):
            raise ReleaseNotesError(f"release tag must start with 'v': {tag!r}")
        if tagged != requested:
            raise ReleaseNotesError(
                f"release tag/version mismatch: tag={tag!r}, version={requested!r}"
            )

    heading = re.compile(rf"^## \[{re.escape(requested)}\](?:\s+[^\n]*)?$", re.MULTILINE)
    matches = list(heading.finditer(changelog))
    if len(matches) != 1:
        qualifier = "missing" if not matches else "duplicate"
        raise ReleaseNotesError(f"{qualifier} changelog section for {requested}")

    start = matches[0].start()
    next_heading = re.search(r"^## \[", changelog[matches[0].end() :], re.MULTILINE)
    end = (
        matches[0].end() + next_heading.start()
        if next_heading is not None
        else len(changelog)
    )
    section = changelog[start:end].strip()
    body = section.split("\n", 1)[1].strip() if "\n" in section else ""
    if not body:
        raise ReleaseNotesError(f"empty changelog section for {requested}")
    return section + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release version, e.g. 0.3.2")
    parser.add_argument("--tag", help="optional matching v-prefixed tag, e.g. v0.3.2")
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="changelog path"
    )
    parser.add_argument("--output", type=Path, help="write notes here instead of stdout")
    args = parser.parse_args(argv)

    try:
        notes = extract_release_notes(
            args.changelog.read_text(encoding="utf-8"), args.version, tag=args.tag
        )
    except (OSError, ReleaseNotesError) as exc:
        parser.error(str(exc))

    if args.output is None:
        sys.stdout.write(notes)
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
