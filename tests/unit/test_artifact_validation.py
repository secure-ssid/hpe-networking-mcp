"""Regression tests for fail-closed design-bundle review gates."""

from __future__ import annotations

from hpe_networking_mcp.pipeline.artifact_validation import (
    REQUIRED_BOUNDARY_PHRASE,
    validate_for_review,
)

_BASE_BUNDLE = {
    "ok": True,
    "title": "Pilot",
    "bom": {
        "line_items": [
            {
                "sku": "EX4400-24P",
                "role": "access_switch",
                "quantity": 2,
                "field_labels": {"sku": "official", "role": "operator_input"},
            }
        ],
        "total_units": 2,
    },
    "unresolved_line_items": [],
    "warnings": [],
    "boundary": (
        "review-only artifact: no live device configuration is rendered or pushed; "
        "see docs/juniper-mist-jvd.md"
    ),
}


def _bundle(**overrides):
    bundle = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
              for k, v in _BASE_BUNDLE.items()}
    bundle.update(overrides)
    return bundle


def test_clean_bundle_is_ready_for_review():
    result = validate_for_review(_bundle())
    assert result["ready_for_review"] is True
    assert result["blocking_reasons"] == []
    assert len(result["checked"]) >= 5


def test_unresolved_line_item_blocks_review():
    bundle = _bundle(
        unresolved_line_items=[{"requested_sku": "NOPE", "warning": "no match"}],
        bom={"line_items": [], "total_units": 0},
    )
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any("NOPE" in reason for reason in result["blocking_reasons"])


def test_topology_error_blocks_review():
    bundle = _bundle(topology_error="model.nodes must be a non-empty list")
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any("invalid topology" in reason for reason in result["blocking_reasons"])


def test_empty_bom_blocks_review():
    bundle = _bundle(bom={"line_items": [], "total_units": 0})
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any("no resolved line items" in reason for reason in result["blocking_reasons"])


def test_missing_boundary_statement_blocks_review():
    bundle = _bundle(boundary="some other text")
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any(REQUIRED_BOUNDARY_PHRASE in reason for reason in result["blocking_reasons"])


def test_missing_field_labels_blocks_review():
    bundle = _bundle(
        bom={
            "line_items": [{"sku": "EX4400-24P", "role": "access_switch", "quantity": 1}],
            "total_units": 1,
        }
    )
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any("field_labels" in reason for reason in result["blocking_reasons"])


def test_unresolved_jvd_reference_blocks_review():
    bundle = _bundle(jvd_reference={"ok": False, "warning": "no JVD design with id 'x'"})
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is False
    assert any("jvd_reference did not resolve" in reason for reason in result["blocking_reasons"])


def test_jvd_platform_mismatch_is_advisory_not_blocking():
    bundle = _bundle(
        jvd_reference={
            "ok": True,
            "id": "3stage_dc",
            "compatibility_check": {"any_bom_sku_matches_jvd_platform": False},
            "provenance": {"coverage_note": "campus/branch gap"},
        }
    )
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is True
    assert any("3stage_dc" in note for note in result["advisory_notes"])
    assert any("campus/branch gap" in note for note in result["advisory_notes"])


def test_jvd_platform_match_adds_no_advisory_note():
    bundle = _bundle(
        jvd_reference={
            "ok": True,
            "id": "3stage_dc",
            "compatibility_check": {"any_bom_sku_matches_jvd_platform": True},
            "provenance": {},
        }
    )
    result = validate_for_review(bundle)
    assert result["ready_for_review"] is True
    assert not any("hardware family" in note for note in result["advisory_notes"])


def test_non_dict_bundle_is_rejected():
    result = validate_for_review("not a bundle")
    assert result["ready_for_review"] is False
    assert result["checked"] == []
