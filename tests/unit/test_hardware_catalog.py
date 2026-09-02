"""Regression coverage for local SKU and configuration hardware search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.mcp_servers import catalog
from hpe_networking_mcp.pipeline.clients import hardware_catalog


def _built_catalog(tmp_path: Path) -> Path:
    output = tmp_path / "hardware_catalog.sqlite"
    counts = hardware_catalog.build(db_path=output)
    assert counts["products"] >= 10
    assert counts["aliases"] >= counts["products"]
    return output


def test_exact_sku_lookup_normalizes_case_and_punctuation(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("jl-665a", db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] == "exact_sku"
    assert result["results"][0]["sku"] == "JL665A"
    assert result["results"][0]["source"]["url"].startswith("https://")
    assert "specs" not in result["results"][0]
    assert result["provenance"] == {
        "identity_authority": "official_vendor_source",
        "source_policy": "official Aruba/HPE/Juniper sources only",
        "coverage": "partial",
        "catalog_snapshot_at": "2026-09-02T00:00:00Z",
    }


def test_exact_sku_lookup_for_ex4400_24p(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("ex4400-24p", db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] == "exact_sku"
    assert result["results"][0]["sku"] == "EX4400-24P"
    assert result["results"][0]["port_count"] == 24
    assert "juniper.net" in result["results"][0]["source"]["url"]


def test_exact_sku_lookup_for_ex4100_24p(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("EX4100 24P", db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] == "exact_sku"
    assert result["results"][0]["sku"] == "EX4100-24P"
    assert result["results"][0]["poe"] == "PoE+, 30W per port by default"


def test_24_port_query_disambiguates_ex4400_family(tmp_path):
    """A bare 24-port EX4400 PoE query must not silently prefer the 48-port variants."""
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("EX4400 24 port PoE switch", db_path=db_path)

    assert result["ok"] is True
    skus = {item["sku"] for item in result["results"]}
    assert "EX4400-24P" in skus
    assert all(item["family"] == "EX4400" for item in result["results"])


def test_compare_distinguishes_ex4400_24p_and_ex4100_24p_poe_and_uplinks(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.compare(["EX4400-24P", "EX4100-24P"], db_path=db_path)

    assert result["ok"] is True
    fields = {item["field"]: item for item in result["comparison"]["fields"]}
    assert fields["port_count"]["values"] == {"EX4400-24P": 24, "EX4100-24P": 24}
    assert fields["port_count"]["different"] is False
    assert fields["poe"]["different"] is True
    assert fields["uplinks"]["different"] is True


def test_configuration_search_returns_bounded_labeled_candidates(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("I need SKU for a CX 6300 PoE 48 port", db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] == "candidate"
    assert 1 < len(result["results"]) <= 5
    assert {item["sku"] for item in result["results"]} >= {"JL659A", "JL661A", "JL665A"}
    assert all(item["port_count"] == 48 for item in result["results"])
    assert all("poe" in str(item["poe"]).casefold() for item in result["results"])
    assert all(item["family"] == "CX 6300" for item in result["results"])


def test_vendor_filter_and_detailed_specs(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search(
        "48 port PoE switch", vendor="juniper", include_specs=True, db_path=db_path
    )

    assert result["ok"] is True
    assert result["results"]
    assert all(item["vendor"] == "juniper" for item in result["results"])
    assert all("specs" in item for item in result["results"])


def test_limit_is_bounded_even_for_a_malformed_client_value(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("48 port PoE switch", limit="not-an-int", db_path=db_path)

    assert result["ok"] is True
    assert len(result["results"]) <= 5


def test_no_match_returns_actionable_device_traits(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("a completely imaginary quantum appliance", db_path=db_path)

    assert result["ok"] is False
    assert result["match_type"] == "no_match"
    assert "vendor" in result["guidance"]
    assert "port count" in result["guidance"]


def test_compare_exact_skus_returns_verified_normalized_side_by_side_fields(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.compare(["JL665A", "JL727B"], db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] == "comparison"
    assert [device["sku"] for device in result["devices"]] == ["JL665A", "JL727B"]
    fields = {item["field"]: item for item in result["comparison"]["fields"]}
    assert fields["port_count"]["values"] == {"JL665A": 48, "JL727B": 48}
    assert fields["uplinks"]["values"] == {"JL665A": "4x SFP56", "JL727B": "4x SFP+"}
    assert fields["specs.poe_budget"]["values"] == {"JL665A": "unknown", "JL727B": "370W"}
    assert fields["specs.poe_budget"]["different"] is True


def test_compare_requires_sku_selection_for_an_ambiguous_model_family(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.compare(["CX 6300", "JL727B"], db_path=db_path)

    assert result["ok"] is False
    assert result["match_type"] == "needs_selection"
    issue = result["unresolved"][0]
    assert issue["reason"] == "ambiguous_model"
    assert {candidate["sku"] for candidate in issue["candidates"]} >= {
        "JL659A",
        "JL661A",
        "JL665A",
    }


def test_compare_rejects_duplicate_or_invalid_device_lists(tmp_path):
    db_path = _built_catalog(tmp_path)

    duplicate = hardware_catalog.compare(["JL665A", "jl-665a"], db_path=db_path)
    invalid = hardware_catalog.compare(["JL665A"], db_path=db_path)

    assert duplicate["ok"] is False
    assert "distinct SKUs" in duplicate["error"]
    assert invalid == {
        "ok": False,
        "error": "devices must contain between 2 and 5 SKU or model values",
    }


def test_missing_catalog_returns_build_remedy(tmp_path):
    result = hardware_catalog.search("JL665A", db_path=tmp_path / "missing.sqlite")

    assert result["ok"] is False
    assert "build_hardware_catalog.py" in result["hint"]


def test_untrusted_snapshot_is_rejected_without_replacing_last_verified_catalog(tmp_path):
    db_path = _built_catalog(tmp_path)
    before = db_path.read_bytes()
    seed = json.loads(hardware_catalog.SEED_PATH.read_text(encoding="utf-8"))
    seed["products"][0]["source_url"] = "https://untrusted-reseller.example/JL665A"
    bad_seed = tmp_path / "untrusted.json"
    bad_seed.write_text(json.dumps(seed), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid records"):
        hardware_catalog.build(seed_path=bad_seed, db_path=db_path)

    assert db_path.read_bytes() == before


def test_catalog_router_does_not_steal_general_spec_questions():
    assert hardware_catalog.is_catalog_query("CX6300 specs") is False
    assert hardware_catalog.is_catalog_query("EX4400 hardware specifications") is False
    assert hardware_catalog.is_catalog_query("JL665A") is True
    assert hardware_catalog.is_catalog_query("I need a CX 6300 PoE 48 port SKU") is True


def test_sku_search_is_registered_outside_rag_core():
    import asyncio

    names = {tool.name for tool in asyncio.run(catalog.mcp.list_tools())}

    assert names == {"compare_hardware", "search_hardware_catalog"}


def test_absent_model_family_is_never_substituted_by_another_family(tmp_path):
    """A model outside the partial snapshot must not resolve to a near neighbour.

    "CX 6100 48G Class4 PoE 370W" once returned ok=True with JL727B, whose
    model string ("CX 6200F 48G Class 4 PoE 4SFP+ 370W Switch") differs only by
    the family. Quoting from that ships the wrong product.
    """
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("CX 6100 48G Class4 PoE 370W", db_path=db_path)

    assert result["ok"] is False
    assert result["match_type"] == "model_not_in_catalog"
    assert result["results"] == []
    assert result["requested_models"] == ["CX6100"]
    assert "CX6100" in result["guidance"]
    # Neighbours stay available, but only under a key that cannot be mistaken
    # for an answer to the question that was asked.
    related_skus = {item["sku"] for item in result["related"]}
    assert "JL727B" in related_skus
    assert all(item["family"] != "CX 6100" for item in result["related"])


def test_absent_model_family_reports_uncovered_model_for_port_queries(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("CX 6100 24 port poe", db_path=db_path)

    assert result["ok"] is False
    assert result["match_type"] == "model_not_in_catalog"
    assert result["results"] == []


def test_present_model_family_still_returns_candidates(tmp_path):
    """The absent-model guard must not suppress covered families."""
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("CX 6300 PoE 48 port", db_path=db_path)

    assert result["ok"] is True
    assert result["match_type"] in {"candidate", "best_candidate"}
    assert result["results"]
    assert all(item["family"] == "CX 6300" for item in result["results"])


def test_compare_refuses_a_model_absent_from_the_catalog(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.compare(["CX 6100 48G PoE", "JL665A"], db_path=db_path)

    assert result["ok"] is False
    reasons = {issue["reason"] for issue in result["unresolved"]}
    assert "model_not_in_catalog" in reasons


def test_punctuated_queries_do_not_raise_and_still_match(tmp_path):
    """Raw FTS syntax must never reach SQLite; "Wi-Fi 7" once broke the MATCH."""
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("Wi-Fi 7 access point", db_path=db_path)

    assert result["ok"] is True
    assert {item["sku"] for item in result["results"]} >= {"AP47-US", "AP47E-US"}


def test_candidate_results_carry_partial_coverage_guidance(tmp_path):
    """A partial snapshot must not read as the complete product family.

    Regression: a 3-SKU CX 6300 result was presented as exhaustive and the
    answer then invented a different family as a "newer replacement".
    """
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("CX 6300", db_path=db_path)

    assert result["ok"] is True
    guidance = result["guidance"].casefold()
    assert "not the complete product family" in guidance
    assert "replacement" in guidance
    assert result["catalog"]["coverage"] == "partial"


def test_exact_sku_lookup_also_carries_coverage_guidance(tmp_path):
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.search("JL659A", db_path=db_path)

    assert result["match_type"] == "exact_sku"
    assert "from memory" in result["guidance"].casefold()


def test_comparison_guidance_forbids_inferring_from_the_model_name(tmp_path):
    """Regression: JL659A vs JL661A are both 6300M, so a modular-versus-fixed
    explanation is wrong; the real difference is SmartRate versus 1GbE."""
    db_path = _built_catalog(tmp_path)

    result = hardware_catalog.compare(["JL659A", "JL661A"], db_path=db_path)

    assert result["ok"] is True
    guidance = result["guidance"].casefold()
    assert "do not infer a distinction from the model name" in guidance
    assert "successor or replacement" in guidance
