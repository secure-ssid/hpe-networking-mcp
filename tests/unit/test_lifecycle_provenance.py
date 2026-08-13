"""Tests for ingestion.lifecycle_provenance -- source identity/schema pins."""

from __future__ import annotations

import json

import pytest

from ingestion import lifecycle_provenance as provenance


def test_families_have_committed_pins_on_disk():
    for family in provenance.FAMILIES:
        pin = provenance.load_pin(family)
        assert pin["source_family"] == family
        assert pin["schema_version"] == provenance.SCHEMA_VERSION
        assert isinstance(pin["source_urls"], list)
        assert isinstance(pin["expected_markers"], list)
        assert isinstance(pin["minimum_count"], int)


def test_load_pin_missing_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path / "does-not-exist")
    with pytest.raises(provenance.SourceProvenanceError, match="missing"):
        provenance.load_pin(provenance.SECURITY_ADVISORIES)


def test_load_pin_rejects_invalid_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    path = tmp_path / f"{provenance.SECURITY_ADVISORIES}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": provenance.SCHEMA_VERSION,
                "source_family": provenance.SECURITY_ADVISORIES,
                "source_urls": [],
                "expected_markers": [],
                "minimum_count": "90",
            }
        )
    )

    with pytest.raises(provenance.SourceProvenanceError, match="minimum_count"):
        provenance.load_pin(provenance.SECURITY_ADVISORIES)


def test_minimum_count_comes_from_reviewed_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    pin = provenance.build_pin(
        provenance.SECURITY_ADVISORIES,
        source_urls=["https://example.test/advisories"],
        expected_markers=[],
        minimum_count=123,
        reviewed_at="2026-01-01",
    )
    provenance.write_pin(provenance.SECURITY_ADVISORIES, pin)

    assert provenance.minimum_count(provenance.SECURITY_ADVISORIES) == 123


def test_load_pin_rejects_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    (tmp_path / f"{provenance.SECURITY_ADVISORIES}.json").write_text("{not json")
    with pytest.raises(provenance.SourceProvenanceError, match="invalid"):
        provenance.load_pin(provenance.SECURITY_ADVISORIES)


def test_load_pin_rejects_non_object_json(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    (tmp_path / f"{provenance.SECURITY_ADVISORIES}.json").write_text("[1, 2, 3]")
    with pytest.raises(provenance.SourceProvenanceError, match="must be a JSON object"):
        provenance.load_pin(provenance.SECURITY_ADVISORIES)


def test_build_pin_rejects_unknown_family():
    with pytest.raises(provenance.SourceProvenanceError, match="unknown source-lifecycle family"):
        provenance.build_pin(
            "not-a-family",
            source_urls=[],
            expected_markers=[],
            minimum_count=0,
            reviewed_at="2026-01-01",
        )


def test_build_pin_rejects_negative_minimum():
    with pytest.raises(provenance.SourceProvenanceError, match="minimum_count"):
        provenance.build_pin(
            provenance.SECURITY_ADVISORIES,
            source_urls=["https://example.test/a"],
            expected_markers=[],
            minimum_count=-1,
            reviewed_at="2026-01-01",
        )


def test_build_pin_bounds_source_url_count():
    too_many = [f"https://example.test/{i}" for i in range(provenance.MAX_SOURCE_URLS + 1)]
    with pytest.raises(provenance.SourceProvenanceError, match="source_urls"):
        provenance.build_pin(
            provenance.SECURITY_ADVISORIES,
            source_urls=too_many,
            expected_markers=[],
            minimum_count=1,
            reviewed_at="2026-01-01",
        )


def test_write_pin_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    pin = provenance.build_pin(
        provenance.SECURITY_ADVISORIES,
        source_urls=["https://example.test/a", "https://example.test/b"],
        expected_markers=["marker-a"],
        minimum_count=5,
        reviewed_at="2026-01-01",
        note="test pin",
    )

    path = provenance.write_pin(provenance.SECURITY_ADVISORIES, pin)
    reloaded = provenance.load_pin(provenance.SECURITY_ADVISORIES)

    assert reloaded == pin
    assert json.loads(path.read_text()) == pin


def test_validate_source_identity_accepts_matching_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    pin = provenance.build_pin(
        provenance.SECURITY_ADVISORIES,
        source_urls=["https://example.test/a", "https://example.test/b"],
        expected_markers=[],
        minimum_count=0,
        reviewed_at="2026-01-01",
    )
    provenance.write_pin(provenance.SECURITY_ADVISORIES, pin)

    provenance.validate_source_identity(
        provenance.SECURITY_ADVISORIES,
        ["https://example.test/b", "https://example.test/a"],
    )


def test_validate_source_identity_rejects_changed_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROVENANCE_DIR", tmp_path)
    pin = provenance.build_pin(
        provenance.SECURITY_ADVISORIES,
        source_urls=["https://example.test/a"],
        expected_markers=[],
        minimum_count=0,
        reviewed_at="2026-01-01",
    )
    provenance.write_pin(provenance.SECURITY_ADVISORIES, pin)

    with pytest.raises(provenance.SourceProvenanceError, match="no longer match"):
        provenance.validate_source_identity(
            provenance.SECURITY_ADVISORIES,
            ["https://example.test/unexpected-new-endpoint"],
        )


def test_validate_markers_passes_when_all_present():
    pin = {"expected_markers": ["<ID>", "<Name>"]}
    provenance.validate_markers(
        provenance.HPE_LIFECYCLE_NOTICES, "<Items><ID>1</ID><Name>x</Name></Items>", pin
    )


def test_validate_markers_fails_closed_on_missing_marker():
    pin = {"expected_markers": ["<ID>", "<RenamedTag>"]}
    with pytest.raises(provenance.SourceProvenanceError, match="RenamedTag"):
        provenance.validate_markers(
            provenance.HPE_LIFECYCLE_NOTICES, "<Items><ID>1</ID></Items>", pin
        )


def test_hpe_aruba_current_lifecycle_pin_documents_coverage_gap():
    pin = provenance.load_pin(provenance.HPE_ARUBA_CURRENT_LIFECYCLE)

    assert pin["minimum_count"] == 0
    assert pin["source_urls"] == []
    note = pin["note"].lower()
    assert "no reliable" in note or "no reproducible" in note or pin["note"]
