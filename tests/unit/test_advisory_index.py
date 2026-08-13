from __future__ import annotations

import sqlite3

import pytest

from hpe_networking_mcp.pipeline.clients import advisory_index


@pytest.fixture
def built_index(tmp_path):
    sources = tmp_path / "sources"
    security = sources / "security_advisories"
    lifecycle = sources / "lifecycle_notices"
    juniper = sources / "juniper_security_advisories"
    security.mkdir(parents=True)
    lifecycle.mkdir()
    juniper.mkdir()

    (security / "hpesbnw04987.md").write_text(
        """<!-- source: https://example.test/hpesbnw04987.json -->

# ArubaOS security update

- Advisory ID: HPESBNW04987
- Aggregate severity: Critical
- Initial release: 2025-01-01
- Current release: 2025-02-01
- Status: final

## Product catalog

- ArubaOS 10
- AP-635

## Vulnerabilities

### CVE-2025-12345
"""
    )
    (juniper / "apstra.md").write_text(
        """<!-- source: https://example.test/apstra -->

# Apstra Security Bulletin CVE-2025-13914

Product Affected: Apstra 5.x
Severity: High
"""
    )
    (lifecycle / "123-ap.md").write_text(
        """<!-- source: https://example.test/eos.xml -->

# Aruba AP lifecycle notice

- Notice ID: 123
- Product category: Wireless
- Published: 2024-03-01

## Affected and replacement products

- Product SKU: AP-635; Product Description: Campus AP; Replacement Product SKU: AP-655
"""
    )

    db_path = tmp_path / "specs.sqlite"
    counts = advisory_index.build(sources, db_path)
    return db_path, counts


def test_builds_structured_advisory_and_lifecycle_tables(built_index):
    db_path, counts = built_index

    assert counts == {"advisories": 2, "lifecycle_events": 1, "skipped": 0}
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 1


def test_lookup_advisory_by_id_cve_product_and_severity(built_index):
    db_path, _counts = built_index

    by_id = advisory_index.lookup_advisories(
        advisory_id="hpesbnw04987",
        db_path=db_path,
    )
    by_cve = advisory_index.lookup_advisories(
        cve="CVE-2025-12345",
        db_path=db_path,
    )
    by_product = advisory_index.lookup_advisories(
        product="AP-635",
        min_severity="high",
        db_path=db_path,
    )

    assert by_id[0]["severity"] == "Critical"
    assert by_cve[0]["advisory_id"] == "HPESBNW04987"
    assert by_product[0]["products"] == ["ArubaOS 10", "AP-635"]
    assert by_product[0]["cves"] == ["CVE-2025-12345"]


def test_lookup_lifecycle_returns_skus_and_replacement(built_index):
    db_path, _counts = built_index

    rows = advisory_index.lookup_lifecycle("AP-635", db_path=db_path)

    assert rows[0]["notice_id"] == "123"
    assert rows[0]["product_skus"] == ["AP-635"]
    assert rows[0]["replacement_skus"] == ["AP-655"]


def test_lookup_requires_identifiers_and_valid_severity(built_index):
    db_path, _counts = built_index

    with pytest.raises(ValueError, match="provide product"):
        advisory_index.lookup_advisories(db_path=db_path)
    with pytest.raises(ValueError, match="min_severity"):
        advisory_index.lookup_advisories(
            product="Aruba",
            min_severity="urgent",
            db_path=db_path,
        )


def test_missing_structured_tables_has_actionable_error(tmp_path):
    db_path = tmp_path / "specs.sqlite"
    sqlite3.connect(db_path).close()

    with pytest.raises(FileNotFoundError, match="Structured advisory index"):
        advisory_index.lookup_advisories(product="Aruba", db_path=db_path)


@pytest.fixture
def expanded_index(tmp_path):
    """A richer fixture: multiple severities/dates/categories/source families,
    including a Juniper-shaped record with no bullet metadata (mirrors the
    real Juniper Mist/Apstra table-rendered pages) to exercise filters,
    pagination, correlation, and citation-completeness gaps."""
    sources = tmp_path / "sources"
    security = sources / "security_advisories"
    juniper_sec = sources / "juniper_security_advisories"
    lifecycle = sources / "lifecycle_notices"
    juniper_life = sources / "juniper_lifecycle"
    for d in (security, juniper_sec, lifecycle, juniper_life):
        d.mkdir(parents=True)

    (security / "hpesbnw04987.md").write_text(
        """<!-- source: https://example.test/hpesbnw04987.json -->

# ArubaOS security update

- Advisory ID: HPESBNW04987
- Aggregate severity: Critical
- Initial release: 2025-01-01
- Current release: 2025-02-01
- Status: final

## Product catalog

- ArubaOS 10
- AP-635

## Vulnerabilities

### CVE-2025-12345
"""
    )
    (security / "hpesbnw05000.md").write_text(
        """<!-- source: https://example.test/hpesbnw05000.json -->

# Switch firmware update

- Advisory ID: HPESBNW05000
- Aggregate severity: Medium
- Initial release: 2024-06-01
- Current release: 2024-06-15
- Status: final

## Product catalog

- AP-655

## Vulnerabilities

### CVE-2024-99999
"""
    )
    # Real Juniper security bulletins render as plain tables, not the
    # "- key: value" bullet metadata the parser looks for — no severity,
    # status, or release date is extracted. This mirrors that exactly.
    (juniper_sec / "apstra.md").write_text(
        """<!-- source: https://example.test/apstra -->

# Apstra Security Bulletin CVE-2025-13914

Product Affected: Apstra 5.x
Severity: High
"""
    )
    (lifecycle / "123-ap.md").write_text(
        """<!-- source: https://example.test/eos.xml -->

# Aruba AP lifecycle notice

- Notice ID: 123
- Product category: Wireless
- Published: 2024-03-01

## Affected and replacement products

- Product SKU: AP-635; Product Description: Campus AP; Replacement Product SKU: AP-655
"""
    )
    (lifecycle / "200-switch.md").write_text(
        """<!-- source: https://example.test/eos.xml -->

# HP Switch lifecycle notice

- Notice ID: 200
- Product category: Switches
- Published: September 9, 2011

## Affected and replacement products

- Product SKU: J1234A; Product Description: Old switch; Replacement Product SKU: N/A
"""
    )
    # Real Juniper lifecycle pages render as a table too — no bullet
    # metadata, so category/published/SKUs all come back empty.
    (juniper_life / "mist-edge.md").write_text(
        """<!-- source: https://example.test/mist-edge -->

# Juniper Mist Access Points and Mist Edge Dates & Milestones

Product
EOL Announced
Mist Edge
01/01/2024
"""
    )

    db_path = tmp_path / "specs.sqlite"
    advisory_index.build(sources, db_path)
    return db_path


def test_list_advisories_filters_by_source_family_and_date_range(expanded_index):
    db_path = expanded_index

    by_family = advisory_index.list_advisories(source_family="security_advisories", db_path=db_path)
    assert by_family["total_matched"] == 2
    assert {r["advisory_id"] for r in by_family["results"]} == {
        "HPESBNW04987",
        "HPESBNW05000",
    }

    ranged = advisory_index.list_advisories(
        since="2025-01-01", until="2025-12-31", db_path=db_path
    )
    assert ranged["total_matched"] == 1
    assert ranged["results"][0]["advisory_id"] == "HPESBNW04987"

    juniper_only = advisory_index.list_advisories(
        source_family="juniper_security_advisories", db_path=db_path
    )
    # No bullet metadata -> current_release/initial_release are both None,
    # so a date-ranged query never (falsely) includes this record.
    assert juniper_only["total_matched"] == 1
    ranged_excludes_juniper = advisory_index.list_advisories(
        source_family="juniper_security_advisories", since="2000-01-01", db_path=db_path
    )
    assert ranged_excludes_juniper["total_matched"] == 0


def test_list_advisories_min_severity_and_pagination(expanded_index):
    db_path = expanded_index

    critical_only = advisory_index.list_advisories(min_severity="high", db_path=db_path)
    assert [r["advisory_id"] for r in critical_only["results"]] == ["HPESBNW04987"]

    page1 = advisory_index.list_advisories(limit=1, offset=0, db_path=db_path)
    page2 = advisory_index.list_advisories(limit=1, offset=1, db_path=db_path)
    assert page1["total_matched"] == page2["total_matched"] == 3
    assert page1["count"] == 1
    assert page1["results"][0]["advisory_id"] != page2["results"][0]["advisory_id"]

    clamped = advisory_index.list_advisories(limit=10_000, offset=-5, db_path=db_path)
    assert clamped["limit"] == advisory_index.MAX_LIST_LIMIT
    assert clamped["offset"] == 0


def test_list_advisories_cve_filter_treats_like_wildcards_literally(expanded_index):
    db_path = expanded_index

    assert advisory_index.list_advisories(cve="%", db_path=db_path)["total_matched"] == 0
    assert (
        advisory_index.list_advisories(
            cve="CVE-2025-1234_",
            db_path=db_path,
        )["total_matched"]
        == 0
    )


def test_list_advisories_invalid_date_and_severity_raise(expanded_index):
    db_path = expanded_index

    with pytest.raises(ValueError, match="since must be an exact"):
        advisory_index.list_advisories(since="not-a-date", db_path=db_path)
    with pytest.raises(ValueError, match="min_severity"):
        advisory_index.list_advisories(min_severity="urgent", db_path=db_path)


def test_list_lifecycle_events_filters_by_sku_category_and_date(expanded_index):
    db_path = expanded_index

    by_sku = advisory_index.list_lifecycle_events(product_sku="AP-635", db_path=db_path)
    assert by_sku["total_matched"] == 1
    assert by_sku["results"][0]["notice_id"] == "123"

    by_replacement = advisory_index.list_lifecycle_events(
        replacement_sku="AP-655", db_path=db_path
    )
    assert by_replacement["total_matched"] == 1
    assert by_replacement["results"][0]["notice_id"] == "123"

    by_category = advisory_index.list_lifecycle_events(category="Switches", db_path=db_path)
    assert by_category["total_matched"] == 1
    assert by_category["results"][0]["notice_id"] == "200"

    # "September 9, 2011" is an exact-format prose date this diagnostic
    # parses; the range below should include it.
    ranged = advisory_index.list_lifecycle_events(
        since="2011-01-01", until="2011-12-31", db_path=db_path
    )
    assert ranged["total_matched"] == 1
    assert ranged["results"][0]["notice_id"] == "200"

    juniper_only = advisory_index.list_lifecycle_events(
        source_family="juniper_lifecycle", db_path=db_path
    )
    assert juniper_only["total_matched"] == 1
    assert juniper_only["results"][0]["product_skus"] == []


def test_list_lifecycle_sku_filters_treat_like_wildcards_literally(expanded_index):
    db_path = expanded_index

    assert (
        advisory_index.list_lifecycle_events(
            product_sku="%",
            db_path=db_path,
        )["total_matched"]
        == 0
    )
    assert (
        advisory_index.list_lifecycle_events(
            product_sku="AP-63_",
            db_path=db_path,
        )["total_matched"]
        == 0
    )
    assert (
        advisory_index.list_lifecycle_events(
            replacement_sku="AP-65_",
            db_path=db_path,
        )["total_matched"]
        == 0
    )


def test_correlate_advisory_lifecycle_reports_exact_and_unresolved(expanded_index):
    db_path = expanded_index

    result = advisory_index.correlate_advisory_lifecycle(
        advisory_id="HPESBNW04987", db_path=db_path
    )
    assert "not a fuzzy or semantic match" in result["match_basis"]
    assert len(result["advisories"]) == 1
    entry = result["advisories"][0]
    assert entry["advisory_id"] == "HPESBNW04987"
    matched_products = {m["advisory_product"] for m in entry["exact_matches"]}
    assert matched_products == {"AP-635"}
    assert entry["exact_matches"][0]["notice_id"] == "123"
    assert entry["unresolved_products"] == ["ArubaOS 10"]

    # HPESBNW05000's only product (AP-655) is a *replacement* SKU on notice
    # 123, not a product SKU on any notice -- still an exact match, just via
    # the replacement_sku field.
    result2 = advisory_index.correlate_advisory_lifecycle(
        advisory_id="HPESBNW05000", db_path=db_path
    )
    entry2 = result2["advisories"][0]
    assert entry2["exact_matches"][0]["matched_field"] == "replacement_sku"
    assert entry2["unresolved_products"] == []


def test_correlate_advisory_lifecycle_requires_identifier(expanded_index):
    db_path = expanded_index

    with pytest.raises(ValueError, match="provide product, advisory_id, or cve"):
        advisory_index.correlate_advisory_lifecycle(db_path=db_path)


def test_citation_completeness_reveals_juniper_metadata_gap(expanded_index):
    db_path = expanded_index

    result = advisory_index.citation_completeness(db_path=db_path)

    security = result["advisories"]["security_advisories"]
    assert security["total"] == 2
    assert security["severity"] == 2
    assert security["current_release"] == 2

    juniper_sec = result["advisories"]["juniper_security_advisories"]
    assert juniper_sec["total"] == 1
    assert juniper_sec["severity"] == 0
    assert juniper_sec["current_release"] == 0
    # source_url and advisory_id still resolve even without bullet metadata.
    assert juniper_sec["source_url"] == 1
    assert juniper_sec["advisory_id"] == 1

    juniper_life = result["lifecycle_events"]["juniper_lifecycle"]
    assert juniper_life["total"] == 1
    assert juniper_life["published"] == 0
    assert juniper_life["product_skus"] == 0


def test_list_advisories_missing_index_has_actionable_error(tmp_path):
    db_path = tmp_path / "specs.sqlite"
    sqlite3.connect(db_path).close()

    with pytest.raises(FileNotFoundError, match="Structured advisory index"):
        advisory_index.list_advisories(db_path=db_path)


def test_list_lifecycle_events_missing_index_has_actionable_error(tmp_path):
    db_path = tmp_path / "specs.sqlite"
    sqlite3.connect(db_path).close()

    with pytest.raises(FileNotFoundError, match="Structured lifecycle index"):
        advisory_index.list_lifecycle_events(db_path=db_path)


def test_citation_completeness_missing_index_has_actionable_error(tmp_path):
    db_path = tmp_path / "specs.sqlite"
    sqlite3.connect(db_path).close()

    with pytest.raises(FileNotFoundError, match="Structured advisory/lifecycle index"):
        advisory_index.citation_completeness(db_path=db_path)
