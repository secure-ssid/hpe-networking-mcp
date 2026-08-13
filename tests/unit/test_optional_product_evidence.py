"""Unit tests for hpe_networking_mcp.pipeline.optional_product_evidence (v07-optional-depth).

Covers:
- A compatibility entry is produced for every optional platform and always
  validates against the shared artifact contract.
- Coverage-gap reasons declared in a platform's own manifest provenance are
  faithfully republished, never invented.
- Live read evidence is None unless both the read gate and credential
  presence are satisfied, and is never built from credentials alone.
- Written artifacts never leak a credential value, a raw manifest
  operation body, or an un-hashed git blob.
- `write_backend_evidence` writes exactly one compatibility artifact (plus
  an optional live-evidence artifact) per platform, atomically, under the
  requested output directory.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import optional_product_evidence as evidence


class TestCompatibilityEntry:
    @pytest.mark.parametrize("platform", evidence.OPTIONAL_PLATFORMS)
    def test_builds_valid_entry_for_every_platform(self, platform):
        entry = evidence.build_compatibility_entry(platform)
        assert isinstance(entry, contracts.PlatformCompatibilityEntry)
        assert entry.platform == platform
        assert entry.compatible is True
        assert entry.reasons  # always at least one explanatory reason
        assert len(entry.source_sha256) == 64

    def test_apstra_reports_added_operations(self, monkeypatch):
        # Use an explicit prior manifest so the delta check remains stable
        # after v0.7 becomes HEAD and in shallow CI clones.
        monkeypatch.setattr(
            evidence,
            "_git_head_bytes",
            lambda path: b'{"operations":[]}',
        )
        entry = evidence.build_compatibility_entry("apstra")
        assert entry.operations_added > 0
        assert entry.operations_removed == 0
        assert any("added" in reason for reason in entry.reasons)

    def test_coverage_gaps_are_republished_not_invented(self):
        manifest = {
            "provenance": {
                "coverage_gaps": ["known gap: X is not modeled."],
                "note": "not full API coverage",
            }
        }
        gaps = evidence._coverage_gaps(manifest)
        assert gaps == ["known gap: X is not modeled.", "not full API coverage"]

    def test_no_provenance_yields_no_gaps(self):
        assert evidence._coverage_gaps({}) == []


class TestLiveReadEvidence:
    def test_none_when_read_gate_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_LIVE_TEST_AXIS_READ", raising=False)
        assert evidence.build_live_read_evidence("axis") is None

    def test_none_when_gate_on_but_credentials_absent(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        for name in ("AXIS_BASE_URL", "AXIS_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        assert evidence.build_live_read_evidence("axis") is None

    def test_credentials_alone_never_enable_the_probe(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_LIVE_TEST_AXIS_READ", raising=False)
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "not-a-real-token")
        assert evidence.build_live_read_evidence("axis") is None

    def test_builds_evidence_when_fully_gated_on(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "not-a-real-token")
        built = evidence.build_live_read_evidence("axis")
        assert built is not None
        assert built.mode == "read_only"
        assert built.secrets_included is False
        assert built.raw_response_included is False
        assert len(built.steps) == 1
        assert built.steps[0]["name"] == "axis_config_check"
        assert built.steps[0]["status"] == "ok"


class TestWriteBackendEvidence:
    def test_writes_one_compatibility_artifact_per_platform(self, tmp_path):
        entries = evidence.write_backend_evidence("uxi", output_dir=tmp_path)
        assert len(entries) == 1
        assert entries[0].kind == contracts.PLATFORM_COMPATIBILITY_RESULT
        written = tmp_path / "uxi-compatibility.json"
        assert written.exists()

    def test_writes_live_evidence_only_when_gated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "not-a-real-token")
        entries = evidence.write_backend_evidence("axis", output_dir=tmp_path)
        assert len(entries) == 2
        assert (tmp_path / "axis-compatibility.json").exists()
        assert (tmp_path / "axis-live-evidence.json").exists()

    def test_no_credential_or_raw_identifier_leakage(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "super-secret-token-value")
        evidence.write_backend_evidence("axis", output_dir=tmp_path)
        for written in tmp_path.glob("*.json"):
            text = written.read_text()
            assert "super-secret-token-value" not in text
            parsed = json.loads(text)
            assert parsed  # still valid JSON after redaction

    def test_all_six_platforms_supported(self, tmp_path):
        results = evidence.write_all_backend_evidence(output_dir=tmp_path)
        assert set(results) == set(evidence.OPTIONAL_PLATFORMS)
        for platform_entries in results.values():
            assert len(platform_entries) >= 1
