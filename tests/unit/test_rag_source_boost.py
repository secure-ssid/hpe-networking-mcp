"""Unit tests for source-boost re-ranking on the LanceDB search path.

The boost table encodes which sources are more authoritative. It was only ever
applied on the Redis path, so the default LanceDB backend silently ranked
without it — and with one source holding ~80% of the corpus, that source
crowded out more authoritative material.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.mcp_servers.rag import _SOURCE_BOOST, _boost_sources  # noqa: E402


def _hit(source: str, score: float, path: str = "f.md") -> dict:
    return {"text": "t", "source": source, "doc_type": "d",
            "file_path": path, "chunk_index": 0, "score": score}


def test_empty_input_is_safe():
    assert _boost_sources([]) == []


def test_authoritative_source_can_overtake_from_below():
    """A boosted source slightly behind on raw relevance should be promoted;
    that is the entire point of the table. Uses a realistic candidate spread —
    normalisation is relative to the whole set, so a near-tie only stays a
    near-tie when there are other candidates defining the range."""
    hits = [
        _hit("mist_docs", 0.0300),
        _hit("mist_docs", 0.0205),
        _hit("openapi_specs", 0.0200),
        _hit("mist_docs", 0.0100),
    ]
    ranked = _boost_sources(hits)
    assert ranked[0]["source"] == "mist_docs"      # clear relevance leader holds
    assert ranked[1]["source"] == "openapi_specs"  # near-tie resolved by boost


def test_boost_is_bounded_by_the_normalised_range():
    """Boosts must nudge, not dominate: the largest boost (0.16) is a fraction
    of the 0-1 normalised span, so a hit at the bottom of the range cannot leap
    the top one on boost alone."""
    hits = [
        _hit("mist_docs", 0.030),
        _hit("mist_docs", 0.020),
        _hit("openapi_specs", 0.010),
    ]
    assert _boost_sources(hits)[0]["source"] == "mist_docs"
    assert max(_SOURCE_BOOST.values()) < 1.0


def test_boost_does_not_swamp_a_large_relevance_gap():
    """Raw RRF scores bunch near 0.01-0.03, so applying the 0-1 calibrated
    boosts without normalising would sort purely by source. A clearly more
    relevant hit must survive."""
    hits = [_hit("mist_docs", 0.90), _hit("nac_docs", 0.011), _hit("mist_docs", 0.010)]
    assert [h["source"] for h in _boost_sources(hits)][0] == "mist_docs"


def test_relevance_order_preserved_within_a_source():
    hits = [_hit("mist_docs", 0.01, "low.md"), _hit("mist_docs", 0.03, "high.md")]
    assert [h["file_path"] for h in _boost_sources(hits)] == ["high.md", "low.md"]


def test_identical_scores_do_not_divide_by_zero():
    hits = [_hit("mist_docs", 0.02), _hit("openapi_specs", 0.02)]
    out = _boost_sources(hits)
    assert len(out) == 2
    assert out[0]["source"] == "openapi_specs"  # decided by boost alone


def test_unknown_source_gets_no_boost():
    hits = [_hit("mystery_source", 0.02), _hit("nac_docs", 0.02)]
    assert _boost_sources(hits)[0]["source"] == "nac_docs"


def test_boost_ordering_matches_table_priority():
    """Equal relevance means the table's ordering decides the ranking."""
    sources = ["openapi_specs", "developer_docs", "vsg_docs", "nac_docs", "tech_docs"]
    ranked = [h["source"] for h in _boost_sources([_hit(s, 0.02) for s in sources])]
    assert ranked == sorted(sources, key=lambda s: -_SOURCE_BOOST.get(s, 0.0))


# ---------------------------------------------------------------------------
# Cross-vendor gating
#
# The corpus spans two vendors, but every OpenAPI file ingests under the single
# `openapi_specs` source, so Aruba's 26 specs and Juniper's mist.openapi.json
# shared the highest boost in the table. A Mist question therefore promoted an
# Aruba schema above Juniper prose on the strength of that boost alone.
# ---------------------------------------------------------------------------

from hpe_networking_mcp.mcp_servers.rag import (  # noqa: E402
    _CROSS_VENDOR_PENALTY,
    _boost_key,
    _detect_vendor,
)


def test_mist_spec_gets_its_own_boost_key():
    """The Mist spec lives in the Aruba spec folder, so the source label alone
    cannot separate them — the filename has to."""
    assert _boost_key(_hit("openapi_specs", 0.1, "openapi_specs/mist.openapi.json")) == "mist_specs"
    # fetch_mist_openapi.py writes the pinned spec under a hyphenated name;
    # matching only the legacy dotted name silently demoted it to an Aruba spec.
    assert _boost_key(_hit("openapi_specs", 0.1, "openapi_specs/mist-openapi.json")) == "mist_specs"
    assert _boost_key(_hit("openapi_specs", 0.1, "openapi_specs/config-interfaces.json")) == "openapi_specs"


def test_non_spec_sources_keep_their_own_label_as_key():
    assert _boost_key(_hit("mist_docs", 0.1, "mist_docs/a.md")) == "mist_docs"
    assert _boost_key(_hit("nac_docs", 0.1, "nac_docs/a.md")) == "nac_docs"


def test_detects_each_vendor():
    assert _detect_vendor("Mist EX switch multicast") == "juniper"
    assert _detect_vendor("Aruba Central VLAN configuration") == "aruba"


def test_ambiguous_queries_are_not_gated():
    """Naming both vendors, or neither, must not penalise either side —
    comparisons and generic questions should rank on relevance alone."""
    assert _detect_vendor("compare Aruba Central with Juniper Mist") is None
    assert _detect_vendor("how do I configure a vlan for voice traffic") is None


def test_vendor_tokens_do_not_match_inside_unrelated_words():
    """Matching is token-based, so substrings must not trigger a vendor."""
    assert _detect_vendor("excessive broadcast traffic") is None
    assert _detect_vendor("centralised logging") is None


def test_cross_vendor_hit_loses_its_boost_and_is_penalised():
    """The regression that motivated this: an Aruba spec outranking Juniper
    docs on a Mist question purely because openapi_specs carries the top boost."""
    hits = [
        _hit("openapi_specs", 0.0300, "openapi_specs/config-interfaces.json"),
        _hit("mist_docs", 0.0280, "mist_docs/wired.md"),
        _hit("mist_docs", 0.0100, "mist_docs/other.md"),
    ]
    ranked = _boost_sources(hits, "Mist EX switch multicast IGMP snooping")
    assert ranked[0]["source"] == "mist_docs"
    assert ranked[0]["file_path"] == "mist_docs/wired.md"


def test_same_vendor_spec_still_outranks_prose():
    """Gating must not disable boosting outright — within the queried vendor the
    authoritative spec should still be promoted from just behind."""
    hits = [
        _hit("mist_docs", 0.0300, "mist_docs/wired.md"),
        _hit("openapi_specs", 0.0295, "openapi_specs/mist.openapi.json"),
        _hit("mist_docs", 0.0100, "mist_docs/other.md"),
    ]
    ranked = _boost_sources(hits, "Mist multicast API field")
    assert ranked[0]["file_path"] == "openapi_specs/mist.openapi.json"


def test_aruba_query_still_prefers_aruba_specs():
    hits = [
        _hit("mist_docs", 0.0300, "mist_docs/wired.md"),
        _hit("openapi_specs", 0.0250, "openapi_specs/config-interfaces.json"),
        _hit("mist_docs", 0.0100, "mist_docs/other.md"),
    ]
    ranked = _boost_sources(hits, "Aruba Central interface configuration")
    assert ranked[0]["file_path"] == "openapi_specs/config-interfaces.json"


def test_ungated_query_behaves_as_before():
    """With no vendor named, no hit is penalised — scores stay at
    normalised + boost, preserving the pre-existing ranking behaviour."""
    hits = [
        _hit("openapi_specs", 0.0300, "openapi_specs/config-interfaces.json"),
        _hit("mist_docs", 0.0100, "mist_docs/wired.md"),
    ]
    ranked = _boost_sources(hits, "configure a vlan")
    assert all(h["score"] >= 0 for h in ranked)
    assert ranked[0]["source"] == "openapi_specs"


def test_penalty_applies_rather_than_filtering():
    """Vendor detection is a heuristic, so a cross-vendor hit is demoted, not
    dropped — a strong enough match can still surface."""
    hits = [
        _hit("openapi_specs", 0.0300, "openapi_specs/config-interfaces.json"),
        _hit("mist_docs", 0.0100, "mist_docs/wired.md"),
    ]
    ranked = _boost_sources(hits, "Mist multicast")
    assert len(ranked) == 2
    aruba = [h for h in ranked if h["file_path"].endswith("config-interfaces.json")][0]
    assert aruba["score"] == 1.0 - _CROSS_VENDOR_PENALTY


def test_every_manifest_source_has_a_vendor():
    """Vendor gating silently no-ops for any source it does not know, so a
    source added to the manifest without a vendor entry would quietly opt out
    of cross-vendor ranking. openapi_specs is split by filename into
    aruba/juniper spec keys, so the manifest name maps to both."""
    import json

    from hpe_networking_mcp.mcp_servers.rag import _SOURCE_VENDOR

    manifest = json.loads((ROOT / "ingestion" / "source_manifest.json").read_text())
    missing = [
        entry["source"]
        for entry in manifest
        if entry["source"] not in _SOURCE_VENDOR
    ]
    assert not missing, f"sources with no vendor mapping: {missing}"


def test_juniper_lifecycle_sources_are_not_penalised_on_mist_queries():
    """On a near-tie the queried vendor decides. The gap is deliberately small:
    the penalty is not meant to override a clear relevance win."""
    hits = [
        _hit("security_advisories", 0.0300, "security_advisories/a.md"),
        _hit("juniper_security_advisories", 0.0295, "juniper_security_advisories/b.md"),
        _hit("mist_docs", 0.0100, "mist_docs/c.md"),
    ]
    ranked = _boost_sources(hits, "Mist security advisory")
    assert ranked[0]["source"] == "juniper_security_advisories"
