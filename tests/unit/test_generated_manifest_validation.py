from __future__ import annotations

import json

import pytest

from scripts.check_generated_tool_manifests import validate_all, validate_manifest
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import SCHEMA_VERSION


def _manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "demo",
        "source": {"operation_count": 1},
        "operations": [
            {
                "name": "demo_get_health",
                "key": "GET /health",
                "method": "GET",
                "path": "/health",
                "capability": "read",
                "parameters": [],
            }
        ],
    }


def test_validate_manifest_accepts_consistent_manifest(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(_manifest()))

    assert validate_manifest(path) == ("demo", 1)


def test_validate_manifest_rejects_duplicate_names(tmp_path):
    doc = _manifest()
    doc["source"]["operation_count"] = 2
    doc["operations"].append(
        {
            **doc["operations"][0],
            "key": "GET /other",
            "path": "/other",
        }
    )
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(doc))

    with pytest.raises(ValueError, match="duplicate generated name"):
        validate_manifest(path)


def test_validate_all_checks_committed_manifests():
    results = dict(validate_all())

    assert results["mist"] == 1050
