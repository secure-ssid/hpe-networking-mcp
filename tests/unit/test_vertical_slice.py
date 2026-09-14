"""End-to-end regression coverage recovered from the NAS working tree.

Build fresh indexes from the committed catalog and JVD seeds in temporary
directories, without depending on generated production indexes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline.artifact_validation import validate_for_review
from hpe_networking_mcp.pipeline.clients import hardware_catalog, jvd_catalog
from hpe_networking_mcp.pipeline.design_bundle import build_design_bundle


@pytest.fixture(scope="module")
def real_catalog_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("vertical_slice") / "hardware_catalog.sqlite"
    hardware_catalog.build(seed_path=hardware_catalog.SEED_PATH, db_path=db_path)
    return db_path


@pytest.fixture(scope="module")
def real_jvd_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("vertical_slice") / "jvd_index.sqlite"
    jvd_catalog.build(seed_path=jvd_catalog.SEED_PATH, db_path=db_path)
    return db_path


def test_vertical_slice_resolves_real_ex4400_sku(real_catalog_db: Path):
    lookup = hardware_catalog.search("EX4400-24P", vendor="juniper", db_path=real_catalog_db)
    assert lookup["match_type"] == "exact_sku"
    assert lookup["results"][0]["sku"] == "EX4400-24P"


def test_vertical_slice_finds_real_dc_evpn_jvd_design(real_jvd_db: Path):
    lookup = jvd_catalog.search_designs("EVPN VXLAN data center fabric", db_path=real_jvd_db)
    assert lookup["ok"] is True
    assert lookup["results"][0]["id"] == "3stage_dc"


def test_vertical_slice_composes_full_bundle_against_real_indexes(
    real_catalog_db: Path, real_jvd_db: Path
):
    bundle = build_design_bundle(
        title="EX4400-24P access pilot (regression)",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 2}],
        topology={
            "title": "pilot",
            "nodes": [
                {"id": "acc1", "label": "acc1", "role": "access_switch", "vendor": "juniper"},
                {"id": "acc2", "label": "acc2", "role": "access_switch", "vendor": "juniper"},
            ],
            "links": [{"source": "acc1", "target": "acc2", "link_type": "trunk"}],
        },
        jvd_design_id="3stage_dc",
        catalog_db_path=real_catalog_db,
        jvd_db_path=real_jvd_db,
    )
    assert bundle["ok"] is True
    assert bundle["bom"]["line_items"][0]["sku"] == "EX4400-24P"
    assert bundle["bom"]["total_units"] == 2
    assert bundle["topology"]["nodes"][0]["id"] == "acc1"
    ref = bundle["jvd_reference"]
    assert ref["id"] == "3stage_dc"
    assert ref["provenance"]["source_commit"] == "e4983af7b12663e935cf30c3f369d2d144044718"
    assert ref["compatibility_check"]["any_bom_sku_matches_jvd_platform"] is False
    assert any("directional guidance only" in warning for warning in bundle["warnings"])


def test_vertical_slice_review_gate_reports_ready_with_advisory_note(
    real_catalog_db: Path, real_jvd_db: Path
):
    bundle = build_design_bundle(
        title="EX4400-24P access pilot (review gate regression)",
        line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 2}],
        jvd_design_id="3stage_dc",
        catalog_db_path=real_catalog_db,
        jvd_db_path=real_jvd_db,
    )
    review = validate_for_review(bundle)
    assert review["ready_for_review"] is True
    assert review["blocking_reasons"] == []
    assert any("3stage_dc" in note for note in review["advisory_notes"])
