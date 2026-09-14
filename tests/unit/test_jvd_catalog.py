"""Regression tests for the local JVD structured design index client."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline.clients import jvd_catalog


def _write_seed(path: Path, designs: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_repo": "Juniper/jvd",
                "source_commit": "e4983af7b12663e935cf30c3f369d2d144044718",
                "source_license": "Apache-2.0",
                "snapshot_at": "2026-09-02T15:24:30Z",
                "coverage": "partial",
                "coverage_note": (
                    "Campus and branch are documented externally on juniper.net, "
                    "not as structured entries in this repository's portal data."
                ),
                "designs": designs,
            }
        ),
        encoding="utf-8",
    )


def _design(**overrides) -> dict:
    base = {
        "id": "3stage_dc",
        "name": "3-Stage Data Center",
        "area": "Data Center",
        "description": "EVPN/VXLAN fabric with lean spine underlay.",
        "platforms": ["QFX5120-48Y-8C", "QFX10002-36Q"],
        "os": ["Junos", "Junos EVO"],
        "repo_path": "data_center/adc/3stage_dc",
        "source_url": (
            "https://github.com/Juniper/jvd/tree/"
            "e4983af7b12663e935cf30c3f369d2d144044718/data_center/adc/3stage_dc"
        ),
    }
    base.update(overrides)
    return base


@pytest.fixture
def seeded_index(tmp_path: Path):
    seed_path = tmp_path / "jvd_seed.json"
    db_path = tmp_path / "jvd_index.sqlite"
    _write_seed(
        seed_path,
        [
            _design(),
            _design(
                id="scale_out_firewall_nat",
                name="Scale-Out Firewall and NAT",
                area="Security",
                description="SRX firewall cluster providing scale-out NAT services.",
                platforms=["SRX4700"],
                os=["Junos"],
                repo_path="security/scale_out_firewall_nat",
                source_url=(
                    "https://github.com/Juniper/jvd/tree/"
                    "e4983af7b12663e935cf30c3f369d2d144044718/security/scale_out_firewall_nat"
                ),
            ),
        ],
    )
    result = jvd_catalog.build(seed_path=seed_path, db_path=db_path)
    assert result == {"designs": 2}
    return db_path


def test_build_rejects_seed_missing_pinned_commit(tmp_path: Path):
    seed_path = tmp_path / "jvd_seed.json"
    _write_seed(seed_path, [_design()])
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    del data["source_commit"]
    seed_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="source_commit"):
        jvd_catalog.build(seed_path=seed_path, db_path=tmp_path / "out.sqlite")


def test_build_rejects_non_official_source_url(tmp_path: Path):
    seed_path = tmp_path / "jvd_seed.json"
    _write_seed(seed_path, [_design(source_url="https://example.com/not-jvd")])
    with pytest.raises(ValueError, match="invalid records"):
        jvd_catalog.build(seed_path=seed_path, db_path=tmp_path / "out.sqlite")


def test_build_rejects_duplicate_ids(tmp_path: Path):
    seed_path = tmp_path / "jvd_seed.json"
    _write_seed(seed_path, [_design(), _design()])
    with pytest.raises(ValueError, match="duplicate JVD design id"):
        jvd_catalog.build(seed_path=seed_path, db_path=tmp_path / "out.sqlite")


def test_search_by_area_returns_exact_members(seeded_index: Path):
    result = jvd_catalog.search_designs("", area="Security", db_path=seeded_index)
    assert result["ok"] is True
    assert [item["id"] for item in result["results"]] == ["scale_out_firewall_nat"]


def test_search_by_keyword_finds_evpn_design(seeded_index: Path):
    result = jvd_catalog.search_designs("EVPN VXLAN fabric", db_path=seeded_index)
    assert result["ok"] is True
    assert result["results"][0]["id"] == "3stage_dc"
    assert result["results"][0]["source"]["url"].startswith(
        "https://github.com/Juniper/jvd/tree/e4983af7b12663e935cf30c3f369d2d144044718/"
    )


def test_search_reports_provenance_with_pinned_commit(seeded_index: Path):
    result = jvd_catalog.search_designs("firewall", db_path=seeded_index)
    provenance = result["provenance"]
    assert provenance["identity_authority"] == "official_jvd_repository"
    assert provenance["source_commit"] == "e4983af7b12663e935cf30c3f369d2d144044718"
    assert provenance["coverage"] == "partial"
    assert "campus" in provenance["coverage_note"].lower()


def test_search_no_match_for_campus_returns_explicit_gap_guidance(seeded_index: Path):
    result = jvd_catalog.search_designs("campus wireless access point design", db_path=seeded_index)
    assert result["ok"] is False
    assert result["match_type"] == "no_match"
    assert "area" in result["guidance"].lower()


def test_search_missing_index_reports_build_remedy(tmp_path: Path):
    missing = tmp_path / "does_not_exist.sqlite"
    result = jvd_catalog.search_designs("data center", db_path=missing)
    assert result["ok"] is False
    assert "build_jvd_index.py" in result["hint"]


def test_search_bounds_limit_and_ignores_bad_values(seeded_index: Path):
    result = jvd_catalog.search_designs(
        "", area="Data Center", limit="not-a-number", db_path=seeded_index
    )
    assert result["ok"] is True
    assert len(result["results"]) <= 5


def test_get_design_returns_exact_id_match(seeded_index: Path):
    result = jvd_catalog.get_design("3stage_dc", db_path=seeded_index)
    assert result["ok"] is True
    assert result["match_type"] == "exact_id"
    assert result["result"]["id"] == "3stage_dc"
    assert result["provenance"]["source_commit"] == "e4983af7b12663e935cf30c3f369d2d144044718"


def test_get_design_unknown_id_reports_no_match(seeded_index: Path):
    result = jvd_catalog.get_design("does_not_exist", db_path=seeded_index)
    assert result["ok"] is False
    assert result["match_type"] == "no_match"
    assert "search_designs" in result["guidance"]


def test_build_is_read_only_at_query_time(seeded_index: Path):
    # Query connections must be read-only; writing should fail.
    conn = sqlite3.connect(f"file:{seeded_index}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM designs")
    finally:
        conn.close()
