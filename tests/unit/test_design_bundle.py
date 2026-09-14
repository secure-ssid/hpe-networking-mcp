"""Regression tests for the design bundle composition module.

Exercises the requirements -> SKU -> JVD -> topology -> BOM artifact model
against small local catalog/JVD fixtures (never the production indexes),
verifying deterministic composition, explicit field labeling, and fail-closed
handling of unresolved SKUs / invalid topologies / unknown JVD ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline import design_bundle
from hpe_networking_mcp.pipeline.clients import hardware_catalog, jvd_catalog


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    seed_path = tmp_path / "hw_seed.json"
    db_path = tmp_path / "hw.sqlite"
    seed_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "coverage": "partial",
                "snapshot_at": "2026-09-02T00:00:00Z",
                "sources": [
                    {
                        "id": "juniper-ex4400-models",
                        "url": "https://www.juniper.net/documentation/us/en/hardware/ex4400/"
                        "topics/concept/ex4400-models.html",
                        "title": "EX4400 Models and Specifications",
                    }
                ],
                "products": [
                    {
                        "sku": "EX4400-24P",
                        "aliases": ["EX4400-24P"],
                        "vendor": "juniper",
                        "brand": "Juniper",
                        "model": "EX4400-24P Ethernet Switch",
                        "family": "EX4400",
                        "device_type": "switch",
                        "port_count": 24,
                        "poe": "PoE-bt up to 90W/port",
                        "uplinks": "2x100GbE QSFP28",
                        "summary": "24-port EX4400 access switch.",
                        "specs": {"throughput_gbps": 324},
                        "lifecycle_status": "unknown",
                        "lifecycle": {},
                        "source_url": "https://www.juniper.net/documentation/us/en/hardware/"
                        "ex4400/topics/concept/ex4400-models.html",
                        "source_title": "EX4400 Models and Specifications",
                        "snapshot_at": "2026-09-02T00:00:00Z",
                        "source_status": "verified",
                    },
                    {
                        "sku": "QFX5120-48Y-8C",
                        "aliases": ["QFX5120-48Y-8C"],
                        "vendor": "juniper",
                        "brand": "Juniper",
                        "model": "QFX5120-48Y-8C Ethernet Switch",
                        "family": "QFX5120",
                        "device_type": "switch",
                        "port_count": 48,
                        "poe": None,
                        "uplinks": "8x100GbE QSFP28",
                        "summary": "48-port 25GbE QFX5120 leaf switch.",
                        "specs": {"throughput_gbps": 2000},
                        "lifecycle_status": "unknown",
                        "lifecycle": {},
                        "source_url": "https://www.juniper.net/documentation/us/en/hardware/"
                        "qfx5120/topics/concept/qfx5120-models.html",
                        "source_title": "QFX5120 Models and Specifications",
                        "snapshot_at": "2026-09-02T00:00:00Z",
                        "source_status": "verified",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    hardware_catalog.build(seed_path=seed_path, db_path=db_path)
    return db_path


@pytest.fixture
def jvd_db(tmp_path: Path) -> Path:
    seed_path = tmp_path / "jvd_seed.json"
    db_path = tmp_path / "jvd.sqlite"
    seed_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_repo": "Juniper/jvd",
                "source_commit": "e4983af7b12663e935cf30c3f369d2d144044718",
                "source_license": "Apache-2.0",
                "snapshot_at": "2026-09-02T15:24:30Z",
                "coverage": "partial",
                "coverage_note": "Campus and Branch are not structured entries here.",
                "designs": [
                    {
                        "id": "3stage_dc",
                        "name": "3-Stage Data Center",
                        "area": "Data Center",
                        "description": "EVPN/VXLAN fabric with lean spine underlay.",
                        "platforms": ["QFX5120-48Y-8C"],
                        "os": ["Junos", "Junos EVO"],
                        "repo_path": "data_center/adc/3stage_dc",
                        "source_url": "https://github.com/Juniper/jvd/tree/"
                        "e4983af7b12663e935cf30c3f369d2d144044718/data_center/adc/3stage_dc",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    jvd_catalog.build(seed_path=seed_path, db_path=db_path)
    return db_path


def test_bundle_resolves_exact_sku_with_official_field_labels(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 3}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is True
    item = bundle["bom"]["line_items"][0]
    assert item["sku"] == "EX4400-24P"
    assert item["field_labels"]["sku"] == "official"
    assert item["field_labels"]["role"] == "operator_input"
    assert bundle["bom"]["total_units"] == 3
    assert bundle["unresolved_line_items"] == []


def test_bundle_flags_unresolved_sku_without_dropping_it(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "core_switch", "sku": "NOT-A-REAL-SKU", "quantity": 1}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is False
    assert bundle["bom"]["line_items"] == []
    assert len(bundle["unresolved_line_items"]) == 1
    unresolved = bundle["unresolved_line_items"][0]
    assert unresolved["requested_sku"] == "NOT-A-REAL-SKU"
    assert unresolved["match_type"] == "no_match"
    assert any("NOT-A-REAL-SKU" in warning for warning in bundle["warnings"])


def test_bundle_rejects_non_positive_quantity(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 0}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is False
    assert bundle["unresolved_line_items"][0]["field_labels"]["quantity"] == "unknown"


def test_bundle_attaches_valid_topology(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        topology={
            "title": "pilot",
            "nodes": [{"id": "acc1", "label": "acc1", "role": "access_switch"}],
            "links": [],
        },
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is True
    assert bundle["topology"]["nodes"][0]["id"] == "acc1"


def test_bundle_reports_invalid_topology_without_dropping_bom(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        topology={"title": "broken", "nodes": [], "links": []},
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is False
    assert "topology_error" in bundle
    assert bundle["bom"]["line_items"], "resolved BOM must survive a topology error"


def test_bundle_attaches_jvd_reference_with_pinned_commit(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        jvd_design_id="3stage_dc",
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is True
    ref = bundle["jvd_reference"]
    assert ref["ok"] is True
    assert ref["provenance"]["source_commit"] == "e4983af7b12663e935cf30c3f369d2d144044718"


def test_bundle_flags_mismatched_family_as_advisory_only(catalog_db: Path, jvd_db: Path):
    # EX4400 (campus/branch access) is not among 3stage_dc's official QFX/PTX/ACX
    # platforms -- the compatibility check must flag this without failing the bundle.
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        jvd_design_id="3stage_dc",
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    check = bundle["jvd_reference"]["compatibility_check"]
    assert check["any_bom_sku_matches_jvd_platform"] is False
    assert check["matched_skus"] == []
    assert any("3stage_dc" in warning for warning in bundle["warnings"])
    assert bundle["ok"] is True, "advisory mismatch must not fail an otherwise-valid bundle"


def test_bundle_reports_matching_family_in_compatibility_check(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "leaf", "sku": "QFX5120-48Y-8C", "quantity": 2}],
        jvd_design_id="3stage_dc",
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    check = bundle["jvd_reference"]["compatibility_check"]
    assert check["any_bom_sku_matches_jvd_platform"] is True
    assert check["matched_skus"] == ["QFX5120-48Y-8C"]
    assert not any("no BOM SKU's family" in warning for warning in bundle["warnings"])


def test_bundle_flags_unknown_jvd_id_without_failing_bom(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        jvd_design_id="does_not_exist",
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["jvd_reference"]["ok"] is False
    assert any("does_not_exist" in warning for warning in bundle["warnings"])
    assert bundle["bom"]["line_items"], "resolved BOM must survive a bad jvd_design_id"


def test_bundle_rejects_empty_line_items(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot", line_items=[], catalog_db_path=catalog_db, jvd_db_path=jvd_db
    )
    assert bundle == {"ok": False, "error": "line_items must be a non-empty list"}


@pytest.mark.parametrize(
    "invalid_qty",
    [
        True,
        False,
        1.9,
        1.0,
        float("nan"),
        float("inf"),
        0,
        -1,
        "1.9",
        "2.0",
        "abc",
        "",
        None,
        "\u00b2",
        "1" * 5000,
    ],
)
def test_bundle_rejects_invalid_quantities_without_coercion(
    catalog_db: Path, jvd_db: Path, invalid_qty
):
    bundle = design_bundle.build_design_bundle(
        title="Quantity check",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": invalid_qty}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is False
    assert len(bundle["unresolved_line_items"]) == 1
    unresolved = bundle["unresolved_line_items"][0]
    assert unresolved["field_labels"]["quantity"] == "unknown"
    assert "must be a positive integer" in unresolved["warning"]


def test_bundle_accepts_valid_integer_strings_and_ints(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Valid quantities",
        line_items=[
            {"role": "access_switch", "sku": "EX4400-24P", "quantity": "2"},
            {"role": "access_switch", "sku": "EX4400-24P", "quantity": "+2"},
            {"role": "access_switch", "sku": "EX4400-24P", "quantity": " +2 "},
            {"role": "access_switch", "sku": "EX4400-24P", "quantity": "1_000"},
            {"role": "leaf", "sku": "QFX5120-48Y-8C", "quantity": 3},
        ],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is True
    assert bundle["bom"]["line_items"][0]["quantity"] == 2
    assert bundle["bom"]["line_items"][1]["quantity"] == 2
    assert bundle["bom"]["line_items"][2]["quantity"] == 2
    assert bundle["bom"]["line_items"][3]["quantity"] == 1000
    assert bundle["bom"]["line_items"][4]["quantity"] == 3
    assert bundle["bom"]["total_units"] == 1009


def test_bundle_reports_malformed_item_alongside_valid_items(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Mixed validity",
        line_items=[
            {"role": "access_switch", "sku": "EX4400-24P", "quantity": " +2 "},
            {"role": "leaf", "sku": "QFX5120-48Y-8C", "quantity": "\u00b2"},
        ],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is False
    assert len(bundle["bom"]["line_items"]) == 1
    assert bundle["bom"]["line_items"][0]["sku"] == "EX4400-24P"
    assert len(bundle["unresolved_line_items"]) == 1
    assert bundle["unresolved_line_items"][0]["requested_sku"] == "QFX5120-48Y-8C"


def test_bundle_lookup_efficiency_and_request_local_cache(
    catalog_db: Path, jvd_db: Path, monkeypatch
):
    call_count = 0
    orig_search = hardware_catalog.search

    def counted_search(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_search(*args, **kwargs)

    monkeypatch.setattr(hardware_catalog, "search", counted_search)

    # 100 repeated SKU items
    items = [{"role": f"role_{i}", "sku": "EX4400-24P", "quantity": 1} for i in range(100)]
    bundle1 = design_bundle.build_design_bundle(
        title="Repeated SKUs",
        line_items=items,
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )

    assert bundle1["ok"] is True
    assert len(bundle1["bom"]["line_items"]) == 100
    assert bundle1["bom"]["total_units"] == 100
    assert call_count == 1, (
        f"expected 1 catalog lookup for 100 identical SKUs in 1 request, got {call_count}"
    )

    # Separate request must perform fresh lookup (no global/stateful cache across calls)
    call_count = 0
    bundle2 = design_bundle.build_design_bundle(
        title="Repeated SKUs call 2",
        line_items=[{"role": "access", "sku": "EX4400-24P", "quantity": 1}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle2["ok"] is True
    assert call_count == 1, "separate call must read fresh data from database"

    # Invalid quantities must NOT trigger catalog lookups
    call_count = 0
    bundle_invalid = design_bundle.build_design_bundle(
        title="Invalid qty no lookup",
        line_items=[{"role": "access", "sku": "EX4400-24P", "quantity": True}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle_invalid["ok"] is False
    assert call_count == 0, "invalid quantity must fail before catalog lookup"


def test_bundle_line_items_have_independent_mutable_dicts(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Independent dicts",
        line_items=[
            {"role": "role1", "sku": "EX4400-24P", "quantity": 1},
            {"role": "role2", "sku": "EX4400-24P", "quantity": 2},
        ],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert bundle["ok"] is True
    item1 = bundle["bom"]["line_items"][0]
    item2 = bundle["bom"]["line_items"][1]

    assert item1["role"] == "role1"
    assert item2["role"] == "role2"
    assert item1["quantity"] == 1
    assert item2["quantity"] == 2

    # Mutate item1 dict and verify item2 is unaffected
    item1["source"]["modified"] = True
    assert "modified" not in item2["source"]


def test_bundle_always_states_review_only_boundary(catalog_db: Path, jvd_db: Path):
    bundle = design_bundle.build_design_bundle(
        title="Pilot",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 1}],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )
    assert "no live device configuration is rendered or pushed" in bundle["boundary"]


def test_unresolved_candidates_request_local_lookup_and_independent_records(
    catalog_db: Path, jvd_db: Path, monkeypatch
):
    call_count = 0
    orig_search = hardware_catalog.search

    def counted_search(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_search(*args, **kwargs)

    monkeypatch.setattr(hardware_catalog, "search", counted_search)

    bundle = design_bundle.build_design_bundle(
        title="Unresolved EX4400 query",
        line_items=[
            {"role": "row1", "sku": "EX4400", "quantity": 1},
            {"role": "row2", "sku": "EX4400", "quantity": 1},
        ],
        catalog_db_path=catalog_db,
        jvd_db_path=jvd_db,
    )

    assert bundle["ok"] is False
    assert call_count == 1, (
        f"expected 1 catalog lookup for repeated unresolved query, got {call_count}"
    )

    rows = bundle["unresolved_line_items"]
    assert len(rows) == 2
    assert len(rows[0]["candidates"]) > 0
    assert len(rows[1]["candidates"]) > 0

    rows[0]["candidates"][0]["top_level_custom"] = "mutated_value"
    orig_source_title = rows[1]["candidates"][0]["source"]["title"]
    rows[0]["candidates"][0]["source"]["title"] = "MUTATED SOURCE TITLE"

    assert "top_level_custom" not in rows[1]["candidates"][0]
    assert rows[1]["candidates"][0]["source"]["title"] == orig_source_title


def test_resolved_rows_do_not_share_nested_lifecycle_records(catalog_db: Path):
    seed_path = catalog_db.parent / "hw_seed.json"
    seed = json.loads(seed_path.read_text())
    seed["products"][0]["lifecycle"] = {
        "evidence": {"references": [{"title": "Vendor lifecycle notice"}]}
    }
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    hardware_catalog.build(seed_path=seed_path, db_path=catalog_db)

    bundle = design_bundle.build_design_bundle(
        title="Nested lifecycle isolation",
        line_items=[
            {"role": "first", "sku": "EX4400-24P", "quantity": 1},
            {"role": "second", "sku": "EX4400-24P", "quantity": 1},
        ],
        catalog_db_path=catalog_db,
    )
    assert bundle["ok"] is True
    first, second = bundle["bom"]["line_items"]
    references = first["lifecycle"]["evidence"]["references"]
    references[0]["title"] = "Changed by a consumer"
    references.append({"title": "Another notice"})
    assert second["lifecycle"]["evidence"]["references"] == [{"title": "Vendor lifecycle notice"}]
