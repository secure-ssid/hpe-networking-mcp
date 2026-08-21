"""Unit tests for generated-operation coverage attached to lookup_api hits."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import rag
from hpe_networking_mcp.pipeline.clients import capability_coverage as coverage

CENTRAL_OPS = [
    {
        "name": "get_firmware_compliance",
        "key": "GET /firmware/v1/compliance",
        "method": "GET",
        "path": "/firmware/v1/compliance",
        "capability": "read",
        "operation_id": "getFirmwareCompliance",
        "source_file": "firmware.json",
        "summary": "Get firmware compliance",
    },
    {
        "name": "set_firmware_compliance",
        "key": "POST /firmware/v1/compliance",
        "method": "POST",
        "path": "/firmware/v1/compliance",
        "capability": "write",
        "operation_id": "setFirmwareCompliance",
        "source_file": "firmware.json",
    },
    {
        "name": "list_audit_logs_generated",
        "key": "GET /audit-log/v1/logs",
        "method": "GET",
        "path": "/audit-log/v1/logs",
        "capability": "read",
        "operation_id": "listAuditLogs",
        "source_file": "audit.json",
    },
]


@pytest.fixture(autouse=True)
def _stub_manifests(monkeypatch):
    coverage.clear_caches()

    def exists(platform: str) -> bool:
        return platform in {"central", "glp"}

    def load(platform: str) -> dict:
        if platform == "central":
            return {"operations": CENTRAL_OPS}
        return {"operations": []}

    monkeypatch.setattr(coverage, "manifest_exists", exists)
    monkeypatch.setattr(coverage, "load_manifest", load)
    yield
    coverage.clear_caches()


def test_family_id_skips_version_segment():
    assert coverage.family_id("/firmware/v1/compliance") == "firmware"
    assert coverage.family_id("/v1alpha1/sites") == "sites"
    assert coverage.family_id("/") == "unknown"


def test_lookup_operation_maps_generated_tool():
    record = coverage.lookup_operation("GET", "/firmware/v1/compliance")
    assert record is not None
    assert record["generated_tool"] == "get_firmware_compliance"
    assert record["classification"] == "generated-only"
    assert record["router_profile"] == "opt-in"
    assert record["family"] == "firmware"


def test_parse_exact_api_query_accepts_path_and_camelcase_operation_id():
    assert coverage.parse_exact_api_query("GET /firmware/v1/compliance") == (
        "endpoint",
        ("GET", "/firmware/v1/compliance"),
    )
    assert coverage.parse_exact_api_query("getFirmwareCompliance") == (
        "operation_id",
        ("getFirmwareCompliance",),
    )
    assert coverage.parse_exact_api_query("create_vlan") is None
    assert coverage.parse_exact_api_query("create vlan") is None
    assert coverage.parse_exact_api_query("VLAN") is None


def test_lookup_exact_query_resolves_path_and_operation_id():
    by_path = coverage.lookup_exact_query("GET /firmware/v1/compliance")
    by_id = coverage.lookup_exact_query("getFirmwareCompliance")
    assert by_path is not None and by_id is not None
    assert by_path["generated_tool"] == by_id["generated_tool"] == "get_firmware_compliance"
    assert coverage.lookup_exact_query("create vlan") is None


def test_annotate_lookup_hits_adds_coverage_without_rewriting_contract_keys():
    hits = [
        {
            "text": "GET /firmware/v1/compliance — Get firmware compliance",
            "source": "openapi_specs",
            "file_path": "openapi_specs/firmware.json#GET /firmware/v1/compliance",
            "kind": "endpoint",
            "score": 100,
        },
        {
            "text": "Schema CdaAuthProfile.auth-type",
            "source": "openapi_specs",
            "file_path": "openapi_specs/cda-auth-profile.json#CdaAuthProfile.auth-type",
            "kind": "enum",
            "score": 4,
        },
        {
            "text": "GET /missing/v1/thing",
            "source": "openapi_specs",
            "file_path": "openapi_specs/other.json#GET /missing/v1/thing",
            "kind": "endpoint",
            "score": 3,
        },
    ]

    annotated = coverage.annotate_lookup_hits(hits)
    assert set(hits[0]) == {"text", "source", "file_path", "kind", "score"}
    assert annotated[0]["coverage"]["generated_tool"] == "get_firmware_compliance"
    assert annotated[0]["coverage"]["classification"] == "generated-only"
    assert "coverage" not in annotated[1]
    assert annotated[2]["coverage"]["classification"] == "indexed-only"
    assert annotated[2]["coverage"]["generated_tool"] is None


def test_list_families_pages_and_includes_protocol_only_streaming():
    page = coverage.list_families("central", limit=1, offset=0)
    assert page["family_count"] == 2
    assert page["families"][0]["family"] == "audit-log"
    assert page["protocol_only"][0]["family"] == "streaming"
    assert page["protocol_only"][0]["classification"] == "protocol-only"

    second = coverage.list_families("central", limit=1, offset=1)
    assert second["families"][0]["family"] == "firmware"
    assert second["families"][0]["operation_count"] == 2
    assert "get_firmware_compliance" in second["families"][0]["sample_tools"]
    assert "generated operations" in second["families"][0]["summary"]
    assert second["index_digest"]
    mutated = coverage.list_families("central", limit=1, offset=1)
    mutated["families"][0]["family"] = "mutated"
    assert coverage.list_families("central", limit=1, offset=1)["families"][0]["family"] == "firmware"


def test_lookup_api_annotates_generated_hits(monkeypatch):
    monkeypatch.setattr(
        rag.specs_index,
        "lookup",
        lambda query, top_k=10, *, source=None, platform=None, version=None, include_metadata=False: [
            {
                "text": "GET /firmware/v1/compliance",
                "source": "openapi_specs",
                "file_path": "openapi_specs/firmware.json#GET /firmware/v1/compliance",
                "kind": "endpoint",
                "score": 100,
            }
        ],
    )
    hits = rag.lookup_api("GET /firmware/v1/compliance")
    assert hits[0]["coverage"]["generated_tool"] == "get_firmware_compliance"


def test_list_api_families_rejects_unknown_platform():
    result = rag.list_api_families(platform="mist")
    assert "error" in result
