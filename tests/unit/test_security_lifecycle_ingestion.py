"""Security-advisory and lifecycle source parsing tests."""

from __future__ import annotations

import json

import pytest

from ingestion import scrape_security_lifecycle as sl


def test_parse_changes_csv_rejects_path_traversal():
    with pytest.raises(sl.SourceFetchError, match="unsafe advisory path"):
        sl.parse_changes_csv("../secret.json,2026-01-01T00:00:00Z\n")


def test_parse_changes_csv_dedupes_case_insensitive_archive_entries():
    records = sl.parse_changes_csv(
        "2025/ADVISORY.json,2026-01-01T00:00:00Z\n"
        "2025/advisory.json,2026-02-01T00:00:00Z\n"
    )

    assert records == [
        (
            sl.PurePosixPath("2025/advisory.json"),
            "2026-02-01T00:00:00Z",
        )
    ]


def test_render_csaf_advisory_keeps_security_search_fields():
    spec = {
        "document": {
            "title": "AOS security update",
            "aggregate_severity": {"text": "High"},
            "tracking": {
                "id": "HPESBNW00001",
                "initial_release_date": "2026-01-01T00:00:00Z",
                "current_release_date": "2026-01-02T00:00:00Z",
                "status": "final",
                "version": "2",
            },
            "notes": [
                {
                    "title": "Affected Products",
                    "category": "general",
                    "text": "AOS-10 10.7.1 and below.",
                }
            ],
            "references": [
                {"summary": "Original Advisory", "url": "https://example.test/a"}
            ],
        },
        "product_tree": {"branches": [{"name": "AOS-10"}]},
        "vulnerabilities": [
            {
                "cve": "CVE-2026-0001",
                "scores": [
                    {
                        "cvss_v3": {
                            "baseScore": 8.1,
                            "baseSeverity": "HIGH",
                            "vectorString": "CVSS:3.1/AV:N/AC:L",
                        }
                    }
                ],
                "remediations": [
                    {"category": "vendor_fix", "details": "Upgrade to 10.7.2."}
                ],
            }
        ],
    }

    rendered = sl.render_csaf_advisory(spec, "https://example.test/advisory.json")

    assert "HPESBNW00001" in rendered
    assert "CVE-2026-0001" in rendered
    assert "AOS-10 10.7.1 and below" in rendered
    assert "Upgrade to 10.7.2" in rendered
    assert "8.1" in rendered


def test_parse_hpe_lifecycle_xml_groups_sku_rows():
    xml = """\
<Items>
  <Item><ID>1</ID><Type>2</Type><Name>AP family</Name>
    <PubDate>July 1, 2026</PubDate><Desc>End of sale notice.</Desc>
    <LinkA>ap-eos.pdf</LinkA></Item>
  <Item><TableA>AP-1</TableA><TableB>Old AP</TableB>
    <TableC>AP-2</TableC><TableD>New AP</TableD></Item>
  <Item><ID>2</ID><Type>5</Type><Name>ClearPass release</Name></Item>
</Items>
"""

    notices = sl.parse_hpe_lifecycle_xml(xml)

    assert len(notices) == 2
    assert notices[0]["rows"] == [
        {
            "Product SKU": "AP-1",
            "Product Description": "Old AP",
            "Replacement Product SKU": "AP-2",
            "Replacement Product Description": "New AP",
        }
    ]
    rendered = sl.render_hpe_lifecycle_notice(notices[0])
    assert "Product category: Wireless" in rendered
    assert "AP-1" in rendered
    assert "ap-eos.pdf" in rendered


def test_render_juniper_lifecycle_extracts_embedded_tables():
    page = """\
<html><head><title>Mist Dates</title></head><body>
<script>
{"description":'<p>Official lifecycle milestones.</p>',
 "htmlContent":'<table><tr><th>Product</th><th>End of Support</th></tr>
 <tr><td>AP41</td><td>2030-01-01</td></tr></table>'}
</script></body></html>
"""

    rendered = sl.render_juniper_lifecycle_page(page, "https://example.test/mist")

    assert "# Mist Dates" in rendered
    assert "Official lifecycle milestones" in rendered
    assert "AP41" in rendered
    assert "2030-01-01" in rendered


def test_parse_juniper_security_sitemap_filters_to_mist_and_apstra_bulletins():
    sitemap = """\
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Apstra-CVE-1</loc></url>
  <url><loc>https://supportportal.juniper.net/s/article/2020-Security-Bulletin-Mist-CVE-2</loc></url>
  <url><loc>https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Junos-CVE-3</loc></url>
  <url><loc>https://supportportal.juniper.net/s/article/Mist-troubleshooting</loc></url>
</urlset>
"""

    urls = sl.parse_juniper_security_sitemap(sitemap)

    assert urls == {
        "https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Apstra-CVE-1",
        "https://supportportal.juniper.net/s/article/2020-Security-Bulletin-Mist-CVE-2",
    }


def test_parse_juniper_security_sitemap_index_returns_current_topicarticle_child():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topic-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-view-1.xml</loc></sitemap>
</sitemapindex>
"""

    children = sl.parse_juniper_security_sitemap_index(index)

    assert children == ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"]


def test_parse_juniper_security_sitemap_index_returns_multiple_topicarticle_children():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-2.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""

    children = sl.parse_juniper_security_sitemap_index(index)

    # Deterministic: sorted, not insertion order.
    assert children == [
        "https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml",
        "https://supportportal.juniper.net/s/sitemap-topicarticle-2.xml",
    ]


def test_parse_juniper_security_sitemap_index_ignores_unrelated_children():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topic-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-view-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""

    children = sl.parse_juniper_security_sitemap_index(index)

    assert children == ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"]


def test_parse_juniper_security_sitemap_index_dedupes_duplicate_children():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""

    children = sl.parse_juniper_security_sitemap_index(index)

    assert children == ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"]


def test_parse_juniper_security_sitemap_index_requires_topicarticle_match():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topic-1.xml</loc></sitemap>
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-view-1.xml</loc></sitemap>
</sitemapindex>
"""

    with pytest.raises(sl.SourceFetchError, match="no longer lists any topic-article"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_malformed_xml():
    with pytest.raises(sl.SourceFetchError, match="invalid Juniper sitemap index XML"):
        sl.parse_juniper_security_sitemap_index("<sitemapindex><not-closed>")


def test_parse_juniper_security_sitemap_index_rejects_wrong_root():
    urlset = """\
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></url>
</urlset>
"""
    with pytest.raises(sl.SourceFetchError, match="unexpected Juniper sitemap index root"):
        sl.parse_juniper_security_sitemap_index(urlset)


def test_parse_juniper_security_sitemap_index_rejects_missing_namespace():
    # Same shape, no default xmlns -- must not be silently accepted.
    index = (
        "<sitemapindex>"
        "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    with pytest.raises(sl.SourceFetchError, match="unexpected Juniper sitemap index root"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_off_host_child():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://evil.example.test/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="outside the reviewed"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_non_https_child():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>http://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="not HTTPS"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_embedded_credentials():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://user:pass@supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="outside the reviewed|embeds credentials"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_query_string():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml?x=1</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="query string or fragment"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_fragment():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml#frag</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="query string or fragment"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_path_traversal():
    index = """\
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://supportportal.juniper.net/s/../../sitemap-topicarticle-1.xml</loc></sitemap>
</sitemapindex>
"""
    with pytest.raises(sl.SourceFetchError, match="path traversal"):
        sl.parse_juniper_security_sitemap_index(index)


def test_parse_juniper_security_sitemap_index_rejects_too_many_children():
    children = "".join(
        f"<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topic-{i}.xml</loc></sitemap>"
        for i in range(sl.MAX_JUNIPER_SITEMAP_INDEX_CHILDREN + 1)
    )
    index = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{children}</sitemapindex>"
    )
    with pytest.raises(sl.SourceFetchError, match="exceeding the reviewed bound"):
        sl.parse_juniper_security_sitemap_index(index)


def test_discover_juniper_security_sitemaps_fetches_and_parses_the_index(monkeypatch):
    index_xml = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>"
        "</sitemapindex>"
    )

    def _fetch(url):
        assert url == sl.JUNIPER_SECURITY_SITEMAP_INDEX_URL
        return index_xml

    monkeypatch.setattr(sl, "fetch_text", _fetch)

    children = sl.discover_juniper_security_sitemaps()

    assert children == ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"]


def test_discover_juniper_security_urls_reads_all_discovered_children(monkeypatch):
    pages = {
        sl.JUNIPER_SECURITY_SITEMAP_INDEX_URL: (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>"
            "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-2.xml</loc></sitemap>"
            "</sitemapindex>"
        ),
        "https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml": (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Apstra-CVE-1</loc></url>"
            "</urlset>"
        ),
        "https://supportportal.juniper.net/s/sitemap-topicarticle-2.xml": (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://supportportal.juniper.net/s/article/2020-Security-Bulletin-Mist-CVE-2</loc></url>"
            "</urlset>"
        ),
    }
    monkeypatch.setattr(sl, "fetch_text", lambda url: pages[url])

    urls = sl.discover_juniper_security_urls()

    assert urls == [
        "https://supportportal.juniper.net/s/article/2020-Security-Bulletin-Mist-CVE-2",
        "https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Apstra-CVE-1",
    ]


def test_render_juniper_security_requires_article_body():
    rendered = sl.render_juniper_security_advisory(
        "Apstra advisory",
        "Product Affected\nApstra\nSeverity\nHigh\nCVE-2026-0001",
        "https://example.test/advisory",
    )
    assert "CVE-2026-0001" in rendered

    with pytest.raises(sl.SourceFetchError, match="did not contain"):
        sl.render_juniper_security_advisory(
            "Shell only", "Log in", "https://example.test/shell"
        )


def test_sync_security_skips_unchanged_cached_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SECURITY_DIR", tmp_path)
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / "a.md").write_text("cached")
    (tmp_path / "_manifest.json").write_text(
        json.dumps({"2026/a.json": "2026-01-01T00:00:00Z"})
    )

    monkeypatch.setattr(
        sl,
        "fetch_text",
        lambda url, **kwargs: "2026/a.json,2026-01-01T00:00:00Z\n",
    )

    fetched, skipped = sl.sync_aruba_security()

    assert (fetched, skipped) == (0, 1)


def test_parse_juniper_eol_index_filters_to_mist_and_apstra_entries():
    page = """\
<script>
var nav = [
{
    "label" : "Juniper Mist Access Points and Mist Edge",
    "url" : "/support/eol/product/juniper_ap_series/",
    "target" : "_blank"
}
,
{
    "label" : "Juniper Apstra",
    "url" : "/support/eol/product/apstra/",
    "target" : "_blank"
}
,
{
    "label" : "Juniper Apstra",
    "url" : "/support/eol/software/apstra/",
    "target" : "_blank"
}
,
{
    "label" : "JRIM",
    "url" : "/support/eol/product/jrim_hw/",
    "target" : "_blank"
}
];
</script>
"""

    discovered = sl.parse_juniper_eol_index(page)

    assert set(discovered.values()) == {
        "https://support.juniper.net/support/eol/product/juniper_ap_series/",
        "https://support.juniper.net/support/eol/product/apstra/",
        "https://support.juniper.net/support/eol/software/apstra/",
    }
    assert not any("jrim" in slug for slug in discovered)


def test_parse_juniper_eol_index_requires_mist_or_apstra_entries():
    page = '{"label" : "JRIM", "url" : "/support/eol/product/jrim_hw/"}'
    with pytest.raises(sl.SourceFetchError, match="no longer lists"):
        sl.parse_juniper_eol_index(page)


def test_parse_juniper_eol_index_requires_navigation_entries():
    with pytest.raises(sl.SourceFetchError, match="no EOL navigation entries"):
        sl.parse_juniper_eol_index("<html><body>Something went wrong!</body></html>")


def test_parse_juniper_eol_index_rejects_foreign_host():
    page = (
        '{"label":"Juniper Mist Access Points",'
        '"url":"https://evil.example.test/support/eol/mist/"}'
    )
    with pytest.raises(sl.SourceFetchError, match="outside"):
        sl.parse_juniper_eol_index(page)


def test_extract_hpe_lifecycle_policy_requires_text():
    with pytest.raises(sl.SourceFetchError, match="contained no text"):
        sl.extract_hpe_lifecycle_policy_text("<html><body></body></html>")


def test_discover_juniper_lifecycle_urls_dedupes_against_reviewed_static_set(monkeypatch):
    # The official index re-discovers the exact same three reviewed URLs;
    # the merged result must not duplicate them under a second slug.
    monkeypatch.setattr(
        sl,
        "fetch_text",
        lambda url: (
            '{"label":"Juniper Mist Access Points and Mist Edge",'
            '"url":"/support/eol/product/juniper_ap_series/"}'
            ',{"label":"Juniper Apstra","url":"/support/eol/product/apstra/"}'
            ',{"label":"Juniper Apstra","url":"/support/eol/software/apstra/"}'
        ),
    )

    merged = sl.discover_juniper_lifecycle_urls()

    assert merged == sl.JUNIPER_LIFECYCLE_URLS


def test_discover_juniper_lifecycle_urls_adds_new_official_pages(monkeypatch):
    monkeypatch.setattr(
        sl,
        "fetch_text",
        lambda url: (
            '{"label":"Juniper Mist Access Points and Mist Edge",'
            '"url":"/support/eol/product/juniper_ap_series/"}'
            ',{"label":"Juniper Apstra","url":"/support/eol/product/apstra/"}'
            ',{"label":"Juniper Apstra","url":"/support/eol/software/apstra/"}'
            ',{"label":"Juniper Apstra Cloud","url":"/support/eol/software/apstra-cloud/"}'
        ),
    )

    merged = sl.discover_juniper_lifecycle_urls()

    assert set(sl.JUNIPER_LIFECYCLE_URLS.values()) <= set(merged.values())
    assert "https://support.juniper.net/support/eol/software/apstra-cloud/" in merged.values()
    # Deterministic: sorted by slug, no accidental duplicate URL entries.
    assert list(merged.items()) == sorted(merged.items())
    assert len(set(merged.values())) == len(merged)


def test_hpe_aruba_current_lifecycle_coverage_gap_documents_evidence():
    gap = sl.HPE_ARUBA_CURRENT_LIFECYCLE_COVERAGE_GAP

    assert gap["status"] == "coverage_gap"
    assert gap["source"] == "hpe_aruba_current_lifecycle"
    assert gap["reason"]
    assert len(gap["evidence"]) >= 3
    assert all(isinstance(item, str) and item for item in gap["evidence"])
