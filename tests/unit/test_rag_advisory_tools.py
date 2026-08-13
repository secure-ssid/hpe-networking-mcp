from __future__ import annotations

from hpe_networking_mcp.mcp_servers import rag


def test_lookup_advisory_forwards_structured_filters(monkeypatch):
    captured = {}

    def fake_lookup(**kwargs):
        captured.update(kwargs)
        return [{"advisory_id": "HPESBNW04987"}]

    monkeypatch.setattr(rag.advisory_index, "lookup_advisories", fake_lookup)

    result = rag.lookup_advisory(
        product="AP-635",
        cve="CVE-2025-12345",
        min_severity="high",
        limit=500,
    )

    assert result == [{"advisory_id": "HPESBNW04987"}]
    assert captured == {
        "product": "AP-635",
        "cve": "CVE-2025-12345",
        "advisory_id": None,
        "min_severity": "high",
        "limit": 200,
    }


def test_check_product_lifecycle_clamps_limit(monkeypatch):
    captured = {}

    def fake_lookup(product, *, limit):
        captured.update(product=product, limit=limit)
        return [{"notice_id": "123"}]

    monkeypatch.setattr(rag.advisory_index, "lookup_lifecycle", fake_lookup)

    result = rag.check_product_lifecycle("AP-635", limit=-1)

    assert result == [{"notice_id": "123"}]
    assert captured == {"product": "AP-635", "limit": 1}


def test_structured_tools_return_missing_index_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("rebuild the index")

    monkeypatch.setattr(rag.advisory_index, "lookup_advisories", missing)
    monkeypatch.setattr(rag.advisory_index, "lookup_lifecycle", missing)

    assert rag.lookup_advisory(cve="CVE-2025-12345") == [
        {"error": "rebuild the index"}
    ]
    assert rag.check_product_lifecycle("AP-635") == [
        {"error": "rebuild the index"}
    ]


def test_list_advisories_forwards_filters_and_pagination(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return {"total_matched": 1, "count": 1, "offset": 0, "limit": 20, "results": []}

    monkeypatch.setattr(rag.advisory_index, "list_advisories", fake_list)

    result = rag.list_advisories(
        product="AP-635",
        source_family="security_advisories",
        since="2025-01-01",
        until="2025-12-31",
        min_severity="high",
        limit=20,
        offset=5,
    )

    assert result["total_matched"] == 1
    assert captured == {
        "product": "AP-635",
        "cve": None,
        "advisory_id": None,
        "min_severity": "high",
        "source_family": "security_advisories",
        "since": "2025-01-01",
        "until": "2025-12-31",
        "limit": 20,
        "offset": 5,
    }


def test_list_advisories_returns_error_on_bad_filter(monkeypatch):
    def raises(**kwargs):
        raise ValueError("min_severity must be low, medium, high, or critical")

    monkeypatch.setattr(rag.advisory_index, "list_advisories", raises)

    result = rag.list_advisories(min_severity="urgent")

    assert result == {"error": "min_severity must be low, medium, high, or critical"}


def test_list_lifecycle_events_forwards_filters(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return {"total_matched": 0, "count": 0, "offset": 0, "limit": 20, "results": []}

    monkeypatch.setattr(rag.advisory_index, "list_lifecycle_events", fake_list)

    rag.list_lifecycle_events(
        product_sku="AP-635",
        replacement_sku="AP-655",
        category="Wireless",
        event_type="end-of-sale/end-of-life",
        source_family="lifecycle_notices",
        since="2020-01-01",
        until="2020-12-31",
        limit=50,
        offset=10,
    )

    assert captured == {
        "product": None,
        "product_sku": "AP-635",
        "replacement_sku": "AP-655",
        "category": "Wireless",
        "event_type": "end-of-sale/end-of-life",
        "source_family": "lifecycle_notices",
        "since": "2020-01-01",
        "until": "2020-12-31",
        "limit": 50,
        "offset": 10,
    }


def test_correlate_advisory_lifecycle_forwards_and_returns(monkeypatch):
    captured = {}

    def fake_correlate(**kwargs):
        captured.update(kwargs)
        return {"match_basis": "exact only", "advisories": [{"advisory_id": "HPESBNW04987"}]}

    monkeypatch.setattr(rag.advisory_index, "correlate_advisory_lifecycle", fake_correlate)

    result = rag.correlate_advisory_lifecycle(advisory_id="HPESBNW04987", limit=5)

    assert result["advisories"][0]["advisory_id"] == "HPESBNW04987"
    assert captured == {
        "product": None,
        "advisory_id": "HPESBNW04987",
        "cve": None,
        "limit": 5,
    }


def test_correlate_advisory_lifecycle_returns_error_without_identifier(monkeypatch):
    def raises(**kwargs):
        raise ValueError("provide product, advisory_id, or cve")

    monkeypatch.setattr(rag.advisory_index, "correlate_advisory_lifecycle", raises)

    assert rag.correlate_advisory_lifecycle() == {
        "error": "provide product, advisory_id, or cve"
    }


def test_rag_diagnostics_combines_all_three_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(
        rag.advisory_index,
        "citation_completeness",
        lambda: {"advisories": {}, "lifecycle_events": {}},
    )

    def freshness_missing():
        raise FileNotFoundError("no artifact yet")

    monkeypatch.setattr(rag.rag_diagnostics_client, "freshness_summary", freshness_missing)
    monkeypatch.setattr(
        rag.rag_diagnostics_client,
        "ingestion_delta",
        lambda: {"sources": {"security_advisories": {"status": "indexed"}}},
    )

    result = rag.rag_diagnostics()

    assert result["citation_completeness"] == {"advisories": {}, "lifecycle_events": {}}
    assert result["source_freshness"] == {"error": "no artifact yet"}
    assert result["ingestion_delta"]["sources"]["security_advisories"]["status"] == "indexed"


def test_rag_diagnostics_can_skip_ingestion_delta(monkeypatch):
    monkeypatch.setattr(rag.advisory_index, "citation_completeness", lambda: {})
    monkeypatch.setattr(rag.rag_diagnostics_client, "freshness_summary", lambda: {})

    calls = []
    monkeypatch.setattr(
        rag.rag_diagnostics_client,
        "ingestion_delta",
        lambda: calls.append("called"),
    )

    result = rag.rag_diagnostics(include_ingestion_delta=False)

    assert "ingestion_delta" not in result
    assert calls == []
