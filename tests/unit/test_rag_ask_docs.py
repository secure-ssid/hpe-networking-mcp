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


def test_ask_docs_includes_bounded_context_for_ambiguous_follow_up(monkeypatch):
    calls = []

    def fake_search(question, top_k=3, source=None):
        calls.append((question, top_k, source))
        return [
            {
                "text": "AOS-CX 10.16 adds VSF support for the CX 6100.",
                "source": "aoscx_release_notes",
                "file_path": "aoscx_release_notes/cx6100.md",
                "score": 0.95,
            }
        ]

    monkeypatch.setattr(rag, "search_docs", fake_search)

    out = rag.ask_docs(
        "what about 10.16 code?",
        context="Comparing Juniper EX4000 with Aruba CX 6100.",
    )

    assert out["mode"] == "search_docs"
    assert "Aruba CX 6100" in calls[0][0]
    assert "10.16 code" in calls[0][0]
    assert "VSF support" in out["answer"]


def test_ask_docs_routes_ex4000_to_hardware_catalog():
    out = rag.ask_docs("EX4000 switching capacity")

    assert out["mode"] == "hardware_specs"
    assert out["citations"][0]["file_path"] == "hardware_specs_catalog:ex4000"
    assert "EX4000" in out["answer"]


def test_ask_docs_routes_ex4000_layer_question_to_hardware_catalog():
    out = rag.ask_docs("is 4000 L2 and L3?")

    assert out["mode"] == "hardware_specs"
    assert "Layer 2 and Layer 3" in out["answer"]


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


def test_search_docs_caches_successful_results(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        return [{"text": "cached result", "score": 1.0}]

    rag._SEARCH_CACHE.clear()
    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)
    monkeypatch.setattr(rag.lance_client, "connect", lambda: object())
    monkeypatch.setattr(rag.lance_client, "index_identity", lambda _db: "test-index")

    first = rag.search_docs("cacheable query", top_k=3)
    second = rag.search_docs("  CACHEABLE   QUERY ", top_k=3)

    assert first == second == [{"text": "cached result", "score": 1.0}]
    assert calls == [("cacheable query", 3, None)]
    rag._SEARCH_CACHE.clear()


def test_search_docs_does_not_cache_error_results(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        return [{"error": "index missing"}]

    rag._SEARCH_CACHE.clear()
    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)
    monkeypatch.setattr(rag.lance_client, "connect", lambda: object())
    monkeypatch.setattr(rag.lance_client, "index_identity", lambda _db: "test-index")

    assert rag.search_docs("error query") == [{"error": "index missing"}]
    assert rag.search_docs("error query") == [{"error": "index missing"}]
    assert calls == [("error query", 5, None), ("error query", 5, None)]
    rag._SEARCH_CACHE.clear()


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


def test_search_docs_normalizes_and_deduplicates_comma_source_filter(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_filter):
        calls.append((query, top_k, source_filter))
        return []

    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", fake_search)

    rag.search_docs("query", source="developer_docs, tech_docs,developer_docs")

    assert calls == [("query", 5, ("developer_docs", "tech_docs"))]


def test_search_docs_rejects_malformed_source_filter_without_search(monkeypatch):
    called = False

    def unexpected_search(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(rag, "_BACKEND", "lancedb")
    monkeypatch.setattr(rag, "_search_lancedb", unexpected_search)

    out = rag.search_docs("query", source="developer_docs,,tech_docs")

    assert called is False
    assert out[0]["error"].startswith("invalid source filter")


def test_ask_docs_synthesizes_distinct_evidence_with_aligned_citations(monkeypatch):
    monkeypatch.setattr(
        rag,
        "lookup_api",
        lambda question, top_k=3: [
            {
                "text": "POST /one creates the first resource.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/one.json",
                "score": 1.0,
            },
            {
                "text": "POST /one creates the first resource.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/one-duplicate.json",
                "score": 0.9,
            },
            {
                "text": "GET /two lists the related resources.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/two.json",
                "score": 0.8,
            },
        ],
    )

    out = rag.ask_docs("Which API endpoint and method should I use?", top_k=3)

    assert out["mode"] == "lookup_api"
    assert "POST /one" in out["answer"]
    assert "GET /two" in out["answer"]
    assert len(out["answer"]) <= rag._MAX_EVIDENCE_ANSWER_CHARS
    assert [citation["file_path"] for citation in out["citations"]] == [
        "openapi_specs/one.json",
        "openapi_specs/two.json",
    ]


def test_evidence_boundary_note_absent_when_hits_agree():
    hits = [
        {"source": "aos_techdocs", "platform": "aos-cx", "version": "10.13"},
        {"source": "aos_techdocs", "platform": "aos-cx", "version": "10.13"},
    ]

    assert rag._evidence_boundary_note(hits) == ""


def test_evidence_boundary_note_flags_mixed_versions():
    hits = [
        {"source": "aos_techdocs", "platform": "aos-cx", "version": "10.10"},
        {"source": "aos_techdocs", "platform": "aos-cx", "version": "10.13"},
    ]

    note = rag._evidence_boundary_note(hits)

    assert note.startswith("Boundary:")
    assert "verify applicability" in note


def test_evidence_boundary_note_flags_mixed_platforms_and_sources():
    hits = [
        {"source": "techdocs_html", "platform": "central"},
        {"source": "mist_docs", "platform": "mist"},
    ]

    note = rag._evidence_boundary_note(hits)

    assert note.startswith("Boundary:")


def test_ask_docs_prepends_boundary_note_for_mixed_platform_evidence(monkeypatch):
    monkeypatch.setattr(
        rag,
        "lookup_api",
        lambda question, top_k=3: [
            {
                "text": "POST /one creates the first resource.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/one.json",
                "platform": "central",
                "version": "2.13",
                "score": 1.0,
            },
            {
                "text": "GET /two lists the related resources.",
                "source": "openapi_specs",
                "file_path": "openapi_specs/two.json",
                "platform": "aos-cx",
                "version": "10.13",
                "score": 0.8,
            },
        ],
    )

    out = rag.ask_docs("Which API endpoint and method should I use?", top_k=3)

    assert out["answer"].startswith("Boundary:")
    assert "POST /one" in out["answer"]
    assert "GET /two" in out["answer"]
    assert len(out["answer"]) <= rag._MAX_EVIDENCE_ANSWER_CHARS
    assert out["citations"][0]["platform"] == "central"
    assert out["citations"][1]["platform"] == "aos-cx"


def test_lookup_api_clamps_negative_top_k_to_one(monkeypatch):
    calls = []

    def fake_lookup(query, top_k, **kwargs):
        calls.append((query, top_k, kwargs))
        return []

    monkeypatch.setattr(rag.specs_index, "lookup", fake_lookup)

    rag.lookup_api("wlan endpoint", top_k=-5)

    assert calls == [
        (
            "wlan endpoint",
            1,
            {
                "source": None,
                "platform": None,
                "version": None,
                "include_metadata": False,
            },
        )
    ]


def test_lookup_api_forwards_provenance_filters(monkeypatch):
    captured = {}

    def fake_lookup(query, top_k, **kwargs):
        captured.update(query=query, top_k=top_k, **kwargs)
        return [{"kind": "endpoint"}]

    monkeypatch.setattr(rag.specs_index, "lookup", fake_lookup)

    result = rag.lookup_api(
        "create WLAN",
        top_k=4,
        source="product_specs",
        platform="mist",
        version="2607.1.0",
        include_metadata=True,
    )

    assert result == [{"kind": "endpoint"}]
    assert captured == {
        "query": "create WLAN",
        "top_k": 4,
        "source": "product_specs",
        "platform": "mist",
        "version": "2607.1.0",
        "include_metadata": True,
    }


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
    monkeypatch.setattr(rag, "lookup_advisory", lambda **kwargs: [{"error": "no match"}])
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
    assert out["citations"][0]["source"] == "hardware_specs_catalog"


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


def test_ask_docs_routes_sku_request_to_local_hardware_catalog(monkeypatch):
    catalog_result = {
        "ok": True,
        "match_type": "exact_sku",
        "results": [
            {
                "sku": "JL665A",
                "model": "CX 6300F 48G Class 4 PoE 4SFP56 Switch",
                "port_count": 48,
                "poe": "Class 4 PoE",
                "source": {"url": "https://example.test/cx6300"},
            }
        ],
    }
    monkeypatch.setattr(rag.hardware_catalog, "is_catalog_query", lambda _question: True)
    monkeypatch.setattr(rag.hardware_catalog, "search", lambda *args, **kwargs: catalog_result)
    monkeypatch.setattr(
        rag.hardware_catalog,
        "format_compact_answer",
        lambda _result: "Hardware catalog results:\n- JL665A",
    )

    out = rag.ask_docs("What SKU is a 48 port CX 6300 PoE switch?")

    assert out["mode"] == "hardware_catalog"
    assert "JL665A" in out["answer"]
    assert out["citations"][0]["source_url"] == "https://example.test/cx6300"


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


def _raise_missing_index(*args, **kwargs):
    raise FileNotFoundError("LanceDB docs table missing under /nonexistent/data")


def test_search_docs_missing_index_is_degraded_with_fetch_hint(
    monkeypatch, tmp_path
):
    """A missing prose index consulted nothing -- it must render degraded with
    a state-aware remedy, like the spec index, never as a bare error string or
    an empty result a model could read as "no such documentation"."""
    monkeypatch.setattr(rag, "PROSE_SOURCES_DIR", tmp_path / "no-sources")
    monkeypatch.setattr(rag.lance_client, "connect", _raise_missing_index)

    out = rag.search_docs("query no cache can hold 4f3c2b1a")

    assert out[0]["degraded"] is True
    assert "docs table missing" in out[0]["error"]
    assert "refresh_rag_sources.py --refresh-sources" in out[0]["hint"]


def test_search_docs_missing_index_hint_is_the_build_once_sources_exist(
    monkeypatch, tmp_path
):
    source = tmp_path / "sources" / "tech_docs"
    source.mkdir(parents=True)
    (source / "guide.md").write_text("# Guide")
    monkeypatch.setattr(rag, "PROSE_SOURCES_DIR", tmp_path / "sources")
    monkeypatch.setattr(rag.lance_client, "connect", _raise_missing_index)

    out = rag.search_docs("another uncached query 9e1d7c55")

    assert out[0]["degraded"] is True
    assert "ingest_docs.py" in out[0]["hint"]
    assert "refresh_rag_sources.py" not in out[0]["hint"]


def test_hardware_specs_citation_does_not_claim_datasheet_provenance():
    """hardware_specs.py holds no source URLs, so its citation must not present
    itself as a datasheet. Claiming doc_type "datasheet" with score 1.0 and no
    URL let a 20-entry in-repo dict outrank genuinely cited corpus evidence."""
    out = rag.ask_docs("cx6300 specs")
    citation = out["citations"][0]

    assert citation["source"] == "hardware_specs_catalog"
    assert citation["doc_type"] != "datasheet"
    assert citation["source"] != "hardware_datasheets"
    # The absence of provenance is stated, not left to be inferred.
    assert citation["source_url"] is None
    assert citation["coverage"] == "series-level"


def test_hardware_specs_answer_discloses_it_cannot_confirm_a_sku():
    """A series-level summary must not read as an orderable-part answer."""
    out = rag.ask_docs("cx6300 specs")

    answer = out["answer"].casefold()
    assert "series-level" in answer
    assert "search_hardware_catalog" in answer


def test_uncatalogued_model_is_not_reported_as_a_retryable_server_error():
    """A resolved "no such model" answer must not surface as HTTP 500.

    ResponseEnvelopeMiddleware falls back to 500 for any result carrying an
    `error` key, so a deterministic miss was returned to clients as a server
    fault hinting that "retrying may help" - advice that can only waste calls.
    The middleware already maps status "not_found" to 404; the tool just had
    to say so.
    """
    from hpe_networking_mcp.mcp_servers._middleware.response_envelope import (
        _blocked_status,
    )

    result = rag.lookup_hardware_specs("JL658A")

    assert result["ok"] is False
    assert result["status"] == "not_found"
    assert _blocked_status(result) == (True, 404)
    # A SKU question should be pointed at the tool that can actually answer it.
    assert "search_hardware_catalog" in result["guidance"]


def test_catalogued_model_still_resolves_without_an_error_envelope():
    from hpe_networking_mcp.mcp_servers._middleware.response_envelope import (
        _blocked_status,
    )

    result = rag.lookup_hardware_specs("cx6300")

    assert result["ok"] is True
    assert _blocked_status(result) == (False, None)
