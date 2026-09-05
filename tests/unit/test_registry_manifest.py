"""The MCP Registry manifest is generated on the current schema and valid."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"


def _manifest() -> dict[str, object]:
    from scripts.gen_integrations import build_registry_manifest

    return json.loads(build_registry_manifest()["server.json"])


def test_registry_manifest_uses_the_current_schema_and_project_version() -> None:
    manifest = _manifest()
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert manifest["$schema"] == SCHEMA_URL
    assert "2025-09-29" not in json.dumps(manifest)
    assert manifest["version"] == project["version"]
    (package,) = manifest["packages"]  # type: ignore[misc]
    assert package["version"] == project["version"]
    assert package["registryType"] == "pypi" and package["identifier"] == "talkthrough-mcp"
    assert package["transport"] == {"type": "stdio"}
    assert len(str(manifest["description"])) <= 100
    env_names = {item["name"] for item in package["environmentVariables"]}
    assert "TALKTHROUGH_MAX_DOWNLOAD_BYTES" in env_names


def test_registry_manifest_validates_against_the_vendored_schema() -> None:
    schema = json.loads(
        (REPO_ROOT / "tests/fixtures/registry-server.schema-2025-12-11.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$id"] == SCHEMA_URL
    jsonschema.Draft7Validator.check_schema(schema)
    errors = list(jsonschema.Draft7Validator(schema).iter_errors(_manifest()))
    assert errors == [], [error.message for error in errors]


def test_checked_in_server_json_matches_the_generator() -> None:
    on_disk = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    assert on_disk == _manifest()
