from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import rag


def test_ask_docs_uses_lookup_api_for_api_question(monkeypatch):
    monkeypatch.setattr(
        rag,
        "lookup_api",
        lambda question, top_k=3: [
            {
                "text": "POST /network-config/v1alpha1/wlan-ssids creates a WLAN.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/wlan.json#/paths",
                "score": 1.0,
            }
        ],
    )
    monkeypatch.setattr(rag, "search_docs", lambda *args, **kwargs: [])

    out = rag.ask_docs("Which endpoint creates a WLAN?", top_k=3)

    assert out["mode"] == "lookup_api"
    assert "wlan-ssids" in out["answer"]
    assert out["citations"][0]["source"] == "openapi_specs"


def test_ask_docs_falls_back_to_search_docs(monkeypatch):
    monkeypatch.setattr(rag, "lookup_api", lambda question, top_k=3: [])
    monkeypatch.setattr(
        rag,
        "search_docs",
        lambda question, top_k=3, source=None: [
            {
                "text": "Use scope maps to target configuration to sites or groups.",
                "source": "developer_docs",
                "file_path": "developer_docs/scopes.md",
                "score": 0.92,
            }
        ],
    )

    out = rag.ask_docs("How should I target config to a site?", top_k=3)

    assert out["mode"] == "search_docs"
    assert "scope maps" in out["answer"]
    assert out["citations"][0]["file_path"] == "developer_docs/scopes.md"


def test_ask_docs_returns_error_without_citations(monkeypatch):
    monkeypatch.setattr(
        rag,
        "search_docs",
        lambda question, top_k=3, source=None: [{"error": "index missing"}],
    )

    out = rag.ask_docs("How do I configure WLANs?", top_k=3)

    assert out == {"answer": "index missing", "citations": [], "mode": "search_docs"}


def test_search_docs_clamps_negative_top_k_to_one(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        return []

    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)

    rag.search_docs("wlan", top_k=-5)

    assert calls == [("wlan", 1, None)]


@pytest.mark.parametrize(
    ("doc_type", "expected_filter"),
    [
        ("feature-navigator", "feature_navigator"),
        ("security-advisory", ("security_advisories", "juniper_security_advisories")),
        ("lifecycle", ("lifecycle_notices", "juniper_lifecycle")),
    ],
)
def test_search_docs_legacy_doc_type_filters_new_sources(monkeypatch, doc_type, expected_filter):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        source = expected_filter[0] if isinstance(expected_filter, tuple) else expected_filter
        return [
            {
                "text": "result",
                "source": source,
                "doc_type": doc_type,
                "file_path": "source/file.md",
                "score": 1.0,
            }
        ]

    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)

    results = rag.search_docs("query", doc_type=doc_type)

    assert calls == [("query", 5, expected_filter)]
    assert results[0]["doc_type"] == doc_type


def test_search_docs_source_filter_overrides_ambiguous_legacy_doc_type(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        return []

    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)

    rag.search_docs("query", source="juniper_lifecycle", doc_type="lifecycle")

    assert calls == [("query", 5, "juniper_lifecycle")]


def test_lookup_api_clamps_negative_top_k_to_one(monkeypatch):
    calls = []

    def fake_lookup(query, top_k):
        calls.append((query, top_k))
        return []

    monkeypatch.setattr(rag.specs_index, "lookup", fake_lookup)

    rag.lookup_api("wlan endpoint", top_k=-5)

    assert calls == [("wlan endpoint", 1)]


def test_ask_docs_routes_exact_cve_to_lookup_advisory(monkeypatch):
    captured = {}

    def fake_lookup_advisory(**kwargs):
        captured.update(kwargs)
        return [
            {
                "advisory_id": "HPESBNW04987",
                "title": "ArubaOS security update",
                "severity": "Critical",
                "current_release": "2025-02-01",
                "source_url": "https://example.test/hpesbnw04987.json",
                "cves": ["CVE-2025-12345"],
            }
        ]

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(rag, "lookup_advisory", fake_lookup_advisory)
    monkeypatch.setattr(rag, "lookup_api", _unexpected)
    monkeypatch.setattr(rag, "search_docs", _unexpected)

    out = rag.ask_docs("What does CVE-2025-12345 affect?")

    assert out["mode"] == "lookup_advisory"
    assert captured == {"cve": "CVE-2025-12345", "limit": 3}
    assert "HPESBNW04987" in out["answer"]
    assert "Critical" in out["answer"]
    assert out["citations"][0]["advisory_id"] == "HPESBNW04987"
    assert out["citations"][0]["cves"] == ["CVE-2025-12345"]


def test_ask_docs_routes_exact_advisory_id_to_lookup_advisory(monkeypatch):
    captured = {}

    def fake_lookup_advisory(**kwargs):
        captured.update(kwargs)
        return [{"advisory_id": "HPESBNW04987", "title": "ArubaOS security update"}]

    monkeypatch.setattr(rag, "lookup_advisory", fake_lookup_advisory)

    out = rag.ask_docs("Look up advisory hpesbnw04987 please")

    assert out["mode"] == "lookup_advisory"
    assert captured["advisory_id"] == "HPESBNW04987"


def test_ask_docs_falls_back_when_no_advisory_match(monkeypatch):
    monkeypatch.setattr(
        rag, "lookup_advisory", lambda **kwargs: [{"error": "no match"}]
    )
    monkeypatch.setattr(rag, "lookup_api", lambda question, top_k=3: [])
    monkeypatch.setattr(
        rag,
        "search_docs",
        lambda question, top_k=3, source=None: [
            {"text": "prose fallback", "source": "tech_docs", "file_path": "tech_docs/x.md"}
        ],
    )

    out = rag.ask_docs("What does CVE-2099-00000 affect?")

    assert out["mode"] == "search_docs"
    assert out["answer"] == "prose fallback"


def test_citation_includes_bounded_lifecycle_and_advisory_fields():
    citation = rag._citation(
        {
            "file_path": "lifecycle_notices/123-ap.md",
            "source_family": "lifecycle_notices",
            "notice_id": "123",
            "category": "Wireless",
            "event_type": "end-of-sale/end-of-life",
            "published": "2024-03-01",
            "product_skus": ["AP-635"],
            "replacement_skus": ["AP-655"],
        }
    )

    assert citation["notice_id"] == "123"
    assert citation["category"] == "Wireless"
    assert citation["event_type"] == "end-of-sale/end-of-life"
    assert citation["product_skus"] == ["AP-635"]
    assert citation["replacement_skus"] == ["AP-655"]


def test_citation_omits_empty_list_fields():
    citation = rag._citation({"file_path": "x.md", "cves": [], "product_skus": []})

    assert "cves" not in citation
    assert "product_skus" not in citation


def test_ask_docs_routes_hardware_specs_query():
    out = rag.ask_docs("cx6300 specs")
    assert out["mode"] == "hardware_specs"
    assert "Aruba CX 6300" in out["answer"]
    assert "880 Gbps" in out["answer"]
    assert "VSF" in out["answer"]
    assert len(out["citations"]) == 1
    assert out["citations"][0]["source"] == "hardware_datasheets"


def test_ask_docs_routes_juniper_hardware_query():
    out = rag.ask_docs("ex4400 hardware specs")
    assert out["mode"] == "hardware_specs"
    assert "EX4400" in out["answer"]
    assert "Virtual Chassis" in out["answer"]


def test_hardware_specs_citation_is_not_a_fabricated_file_path():
    """The citation must not claim a real file exists (e.g. datasheets/*.pdf)
    when the answer is drawn from the curated hardware_specs.py catalog and
    no such file exists anywhere in the repo or ingestion corpus."""
    out = rag.ask_docs("cx6300 specs")
    file_path = out["citations"][0]["file_path"]
    assert file_path == "hardware_specs_catalog:cx6300"
    assert not file_path.endswith(".pdf")


@pytest.mark.parametrize(
    ("query", "expected_model_text"),
    [
        ("ap505 specs", "AP-505"),
        ("AP-515 specs", "AP-515"),
        ("ap-535 specs", "AP-535"),
        ("545 specs", "AP-545"),
        ("ap555 hardware specs", "AP-555"),
    ],
)
def test_ask_docs_routes_common_aruba_ap_models(query, expected_model_text):
    """Regression test: previously only ap635/ap45 were catalogued, so common
    Aruba Wi-Fi 6 AP queries fell through to search_docs and returned
    unrelated content (e.g. CLI show-command output naming other AP models).
    """
    out = rag.ask_docs(query)
    assert out["mode"] == "hardware_specs"
    assert expected_model_text in out["answer"]
    assert out["citations"][0]["file_path"].startswith("hardware_specs_catalog:")


def test_ask_docs_ap_models_do_not_cross_contaminate():
    """Each AP model must return its own distinct spec, not another model's."""
    out_505 = rag.ask_docs("ap505 specs")
    out_555 = rag.ask_docs("ap555 specs")
    assert "AP-505" in out_505["answer"]
    assert "AP-555" not in out_505["answer"]
    assert "AP-555" in out_555["answer"]
    assert "AP-505" not in out_555["answer"]



def test_lookup_hardware_specs_is_registered_as_an_mcp_tool():
    """Regression test: lookup_hardware_specs was fully implemented (and
    already referenced by name in migration_planner's recommended_tools) but
    had no @mcp.tool decorator, so it was uncallable through find_tool /
    invoke_read_tool despite ask_docs's internal routing working correctly.
    """
    import asyncio

    tools = asyncio.run(rag.mcp.list_tools())
    names = {t.name for t in tools}
    assert "lookup_hardware_specs" in names


def test_lookup_hardware_specs_returns_full_spec_for_known_model():
    out = rag.lookup_hardware_specs("cx6300")
    assert out["ok"] is True
    assert out["model"] == "cx6300"
    assert "specs" in out
    assert "880 Gbps" in out["formatted"]


def test_lookup_hardware_specs_unknown_model_lists_available_models():
    out = rag.lookup_hardware_specs("not-a-real-switch")
    assert out["ok"] is False
    assert "cx6300" in out["available_models"]
    assert "ex4400" in out["available_models"]
