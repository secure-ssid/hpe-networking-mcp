from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.generate_edgeconnect_tools as edgeconnect_cli
from hpe_networking_mcp.mcp_servers.openapi_gen.compatibility import (
    CompatibilityError,
    build_compatibility_report,
    dumps_report,
)
from hpe_networking_mcp.mcp_servers.openapi_gen.ir import SpecParser
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import sha256_bytes

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/edgeconnect_compatibility"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _baseline() -> tuple[dict, bytes]:
    raw = _fixture("baseline_manifest.json")
    return json.loads(raw), raw


def _provenance(raw: bytes) -> dict:
    return {
        "manifest_sha256": sha256_bytes(raw),
        "operation_count": 3,
        "supported_base_paths": ["/"],
        "runtime_auth_schemes": [
            {
                "name": "configurable_header_token",
                "type": "apiKey",
                "in": "header",
                "parameter_name": "X-Auth-Token",
            },
            {
                "name": "authorization_bearer",
                "type": "http",
                "scheme": "bearer",
            },
        ],
    }


def _report(name: str, *, provenance: dict | None = None, expected: str | None = None):
    manifest, raw = _baseline()
    return build_compatibility_report(
        payload=_fixture(name),
        source_name=name,
        baseline_manifest=manifest,
        baseline_manifest_bytes=raw,
        provenance=provenance or _provenance(raw),
        expected_source_sha256=expected,
    )


@pytest.mark.parametrize(
    ("fixture", "document_format"),
    [
        ("compatible_openapi3.json", "OpenAPI 3.0.3"),
        ("compatible_swagger2.json", "Swagger 2.0"),
    ],
)
def test_compatible_swagger_and_openapi_reports_are_deterministic(
    fixture: str, document_format: str
):
    first = _report(fixture)
    second = _report(fixture)

    assert first["verdict"] == {
        "compatible": True,
        "value": "compatible",
        "reasons": [],
    }
    assert first["source"]["document_format"] == document_format
    assert first["source"]["declared_api_version"] == "9.3.1"
    assert first["operations"]["baseline_count"] == 3
    assert first["operations"]["target_count"] == 3
    assert dumps_report(first) == dumps_report(second)


def test_swagger2_normalizes_body_parameters_and_refs_through_shared_ir():
    operation = next(
        item
        for item in SpecParser(json.loads(_fixture("compatible_swagger2.json"))).operations()
        if item.operation_id == "updateWidget"
    )

    assert operation.request_body is not None
    assert operation.request_body.content_type == "application/json"
    assert operation.request_body.schema_type == "object"
    assert operation.parameters[0].name == "id"


def test_changed_report_categorizes_operation_auth_and_path_differences():
    report = _report("changed_openapi3.json")

    assert report["verdict"]["compatible"] is False
    assert report["operations"]["added"] == ["GET /health"]
    assert report["operations"]["removed"] == ["DELETE /legacy"]
    assert [item["operation_id"] for item in report["operations"]["method_changed"]] == [
        "listWidgets"
    ]
    assert [item["operation_id"] for item in report["operations"]["path_changed"]] == [
        "updateWidget"
    ]
    assert report["authentication"]["unsupported_used_scheme_names"] == ["basic"]
    assert report["authentication"]["compatible"] is False


def test_malformed_and_unsupported_documents_fail_closed():
    with pytest.raises(CompatibilityError, match="malformed"):
        _report("malformed.json")
    manifest, raw = _baseline()
    with pytest.raises(CompatibilityError, match="unsupported"):
        build_compatibility_report(
            payload=b'{"openapi":"2.0.0","paths":{}}',
            source_name="unsupported.json",
            baseline_manifest=manifest,
            baseline_manifest_bytes=raw,
            provenance=_provenance(raw),
        )


def test_stale_manifest_and_source_digest_mismatch_fail_closed():
    stale = json.loads(_fixture("stale_provenance.json"))
    stale_report = _report("compatible_openapi3.json", provenance=stale)
    digest_report = _report("compatible_openapi3.json", expected="0" * 64)

    assert stale_report["baseline"]["manifest_digest_matches"] is False
    assert stale_report["verdict"]["compatible"] is False
    assert digest_report["source"]["digest_matches"] is False
    assert digest_report["verdict"]["compatible"] is False


def test_non_root_base_path_fails_closed():
    document = json.loads(_fixture("compatible_openapi3.json"))
    document["servers"] = [{"url": "https://orchestrator.example.com/gms/rest"}]
    manifest, raw = _baseline()
    report = build_compatibility_report(
        payload=json.dumps(document).encode(),
        source_name="base-path.json",
        baseline_manifest=manifest,
        baseline_manifest_bytes=raw,
        provenance=_provenance(raw),
    )

    assert report["base_path"]["target"] == ["/gms/rest"]
    assert report["base_path"]["compatible"] is False
    assert report["verdict"]["compatible"] is False


def test_cli_never_generates_without_explicit_flag(monkeypatch, capsys):
    manifest, raw = _baseline()
    provenance = _provenance(raw)
    generated: list[Path] = []
    source = FIXTURES / "compatible_openapi3.json"

    monkeypatch.setattr(
        edgeconnect_cli, "_load_baseline", lambda: (manifest, raw, provenance)
    )
    monkeypatch.setattr(
        edgeconnect_cli,
        "_generate",
        lambda path, payload, baseline, provenance: generated.append(path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["generate_edgeconnect_tools.py", "--source", str(source)],
    )
    assert edgeconnect_cli.main() == 0
    assert generated == []

    monkeypatch.setattr(
        "sys.argv",
        ["generate_edgeconnect_tools.py", "--source", str(source), "--generate"],
    )
    assert edgeconnect_cli.main() == 0
    assert generated == [source]
    capsys.readouterr()


def test_committed_edgeconnect_baseline_digest_and_1216_operations_are_current():
    manifest_path = ROOT / "src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/edgeconnect.json"
    provenance_path = ROOT / "src/hpe_networking_mcp/mcp_servers/openapi_gen/provenance/edgeconnect.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    provenance = json.loads(provenance_path.read_text())

    paths: dict[str, dict] = {}
    for operation in manifest["operations"]:
        paths.setdefault(operation["path"], {})[operation["method"].lower()] = {
            "operationId": operation.get("operation_id"),
            "responses": {"200": {"description": "fixture"}},
        }
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Target Orchestrator", "version": "9.3+"},
        "components": {
            "securitySchemes": {
                "token": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Auth-Token",
                }
            }
        },
        "security": [{"token": []}],
        "paths": paths,
    }
    report = build_compatibility_report(
        payload=json.dumps(document, sort_keys=True).encode(),
        source_name="local-target.json",
        baseline_manifest=manifest,
        baseline_manifest_bytes=manifest_raw,
        provenance=provenance,
    )

    assert report["baseline"]["manifest_digest_matches"] is True
    assert report["operations"]["baseline_count"] == 1216
    assert report["operations"]["target_count"] == 1216
    assert report["verdict"]["compatible"] is True
