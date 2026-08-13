"""Tests for scripts.check_security_lifecycle_drift.

Covers the five distinguished source states (fresh/stale/unavailable/
changed/coverage_gap), backward-compatible check_sources() counts,
provenance-mismatch rejection, the bounded/redacted freshness artifact,
and no secret/tenant leakage in the written artifact.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from ingestion import lifecycle_provenance as provenance
from ingestion import scrape_security_lifecycle as sources
from scripts import check_security_lifecycle_drift as drift

# ---------------------------------------------------------------------------
# Aruba security advisories
# ---------------------------------------------------------------------------


def test_evaluate_aruba_security_fresh(monkeypatch):
    monkeypatch.setattr(sources, "fetch_text", lambda url: "csv")
    monkeypatch.setattr(
        sources, "parse_changes_csv", lambda text: [object()] * drift.MIN_ARUBA_ADVISORIES
    )

    entry = drift.evaluate_aruba_security()

    assert entry["status"] == drift.STATUS_FRESH
    assert entry["count"] == drift.MIN_ARUBA_ADVISORIES
    assert entry["drift_detected"] is False


def test_evaluate_aruba_security_unavailable_on_fetch_failure(monkeypatch):
    def _raise(url):
        raise sources.SourceFetchError(f"failed to fetch {url}: connection refused")

    monkeypatch.setattr(sources, "fetch_text", _raise)

    entry = drift.evaluate_aruba_security()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert entry["count"] == 0
    assert "connection refused" in entry["detail"]


def test_evaluate_aruba_security_changed_on_parse_failure(monkeypatch):
    monkeypatch.setattr(sources, "fetch_text", lambda url: "garbled,csv,too,many,columns")

    def _raise(text):
        raise sources.SourceFetchError("invalid CSAF changes.csv row")

    monkeypatch.setattr(sources, "parse_changes_csv", _raise)

    entry = drift.evaluate_aruba_security()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "invalid CSAF changes.csv row" in entry["detail"]


def test_evaluate_aruba_security_stale_below_minimum(monkeypatch):
    monkeypatch.setattr(sources, "fetch_text", lambda url: "csv")
    monkeypatch.setattr(sources, "parse_changes_csv", lambda text: [object()] * 3)

    entry = drift.evaluate_aruba_security()

    assert entry["status"] == drift.STATUS_STALE
    assert entry["count"] == 3
    assert "below minimum" in entry["detail"]


def test_evaluate_aruba_security_changed_on_provenance_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "fetch_text", lambda url: "csv")
    monkeypatch.setattr(
        sources, "parse_changes_csv", lambda text: [object()] * drift.MIN_ARUBA_ADVISORIES
    )

    def _raise(family, actual_urls):
        raise provenance.SourceProvenanceError(f"{family} source URLs no longer match")

    monkeypatch.setattr(provenance, "validate_source_identity", _raise)

    entry = drift.evaluate_aruba_security()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "no longer match" in entry["detail"]


# ---------------------------------------------------------------------------
# HPE lifecycle notices -- includes the structural marker check
# ---------------------------------------------------------------------------


def test_evaluate_hpe_lifecycle_fresh(monkeypatch):
    monkeypatch.setattr(
        sources,
        "fetch_text",
        lambda url: (
            "<Items><ID>1</ID></Items>"
            if url == sources.HPE_LIFECYCLE_XML
            else "<html><body>Lifecycle policy</body></html>"
        ),
    )
    monkeypatch.setattr(sources, "fetch_bytes", lambda url: b"pdf")
    monkeypatch.setattr(provenance, "validate_markers", lambda family, text: None)
    monkeypatch.setattr(
        sources,
        "extract_aruba_hardware_eos_text",
        lambda value: "hardware",
    )
    monkeypatch.setattr(
        sources,
        "parse_hpe_lifecycle_xml",
        lambda text: [object()] * drift.MIN_HPE_LIFECYCLE_NOTICES,
    )

    entry = drift.evaluate_hpe_lifecycle()

    assert entry["status"] == drift.STATUS_FRESH


def test_juniper_lifecycle_source_identity_drift_is_changed(monkeypatch):
    pages = {
        "mist": "https://support.juniper.net/support/eol/product/juniper_ap_series/"
    }
    monkeypatch.setattr(
        sources,
        "discover_juniper_lifecycle_urls",
        lambda: pages,
    )
    monkeypatch.setattr(
        sources,
        "fetch_text",
        lambda url: "<html>supported</html>",
    )
    monkeypatch.setattr(
        sources,
        "render_juniper_lifecycle_page",
        lambda html, url: "rendered",
    )
    monkeypatch.setattr(drift, "MIN_JUNIPER_LIFECYCLE_PAGES", 1)

    def _raise(family, actual_urls):
        raise provenance.SourceProvenanceError("new official lifecycle page")

    monkeypatch.setattr(provenance, "validate_source_identity", _raise)

    entry = drift.evaluate_juniper_lifecycle()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "new official lifecycle page" in entry["detail"]


def test_evaluate_hpe_lifecycle_changed_when_expected_tags_vanish(monkeypatch):
    # A renamed/removed XML tag does not raise inside parse_hpe_lifecycle_xml
    # itself (it just silently skips the record) -- the marker check is what
    # catches this as "changed" instead of an unexplained "stale".
    monkeypatch.setattr(
        sources,
        "fetch_text",
        lambda url: (
            "<Items><Renamed>1</Renamed></Items>"
            if url == sources.HPE_LIFECYCLE_XML
            else "<html><body>Lifecycle policy</body></html>"
        ),
    )
    monkeypatch.setattr(sources, "fetch_bytes", lambda url: b"pdf")
    monkeypatch.setattr(
        sources,
        "extract_aruba_hardware_eos_text",
        lambda value: "hardware",
    )

    def _raise(family, text):
        raise provenance.SourceProvenanceError(
            f"{family} source no longer contains expected markers"
        )

    monkeypatch.setattr(provenance, "validate_markers", _raise)

    entry = drift.evaluate_hpe_lifecycle()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "no longer contains expected markers" in entry["detail"]


def test_evaluate_hpe_lifecycle_unavailable_on_network_failure(monkeypatch):
    def _raise(url):
        raise sources.SourceFetchError(f"failed to fetch {url}: timed out")

    monkeypatch.setattr(sources, "fetch_text", _raise)

    entry = drift.evaluate_hpe_lifecycle()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert "timed out" in entry["detail"]


# ---------------------------------------------------------------------------
# Juniper lifecycle pages
# ---------------------------------------------------------------------------


def test_evaluate_juniper_lifecycle_fresh(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_lifecycle_urls",
        lambda: dict(sources.JUNIPER_LIFECYCLE_URLS),
    )
    monkeypatch.setattr(sources, "fetch_text", lambda url: "<html></html>")
    monkeypatch.setattr(sources, "render_juniper_lifecycle_page", lambda text, url: "rendered")

    entry = drift.evaluate_juniper_lifecycle()

    assert entry["status"] == drift.STATUS_FRESH
    assert entry["count"] == len(sources.JUNIPER_LIFECYCLE_URLS)


def test_evaluate_juniper_lifecycle_unavailable_when_index_unreachable(monkeypatch):
    def _raise():
        raise sources.SourceFetchError("failed to fetch Juniper EOL index: DNS failure")

    monkeypatch.setattr(sources, "discover_juniper_lifecycle_urls", _raise)

    entry = drift.evaluate_juniper_lifecycle()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert "DNS failure" in entry["detail"]


def test_evaluate_juniper_lifecycle_changed_when_page_content_missing(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_lifecycle_urls",
        lambda: dict(sources.JUNIPER_LIFECYCLE_URLS),
    )
    monkeypatch.setattr(sources, "fetch_text", lambda url: "<html></html>")

    def _raise(text, url):
        raise sources.SourceFetchError(f"no lifecycle description/table content found in {url}")

    monkeypatch.setattr(sources, "render_juniper_lifecycle_page", _raise)

    entry = drift.evaluate_juniper_lifecycle()

    assert entry["status"] == drift.STATUS_CHANGED


# ---------------------------------------------------------------------------
# Juniper security bulletins -- sitemap-index discovery
# ---------------------------------------------------------------------------


def test_evaluate_juniper_security_fresh_with_one_child(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_security_sitemaps",
        lambda: ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"],
    )
    monkeypatch.setattr(sources, "fetch_text", lambda url: "<urlset></urlset>")
    monkeypatch.setattr(
        sources, "parse_juniper_security_sitemap", lambda text: {"https://example.test/bulletin"}
    )

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_FRESH
    assert entry["count"] == 1


def test_evaluate_juniper_security_unavailable_when_index_fetch_fails(monkeypatch):
    def _raise():
        raise sources.SourceFetchError(
            "failed to fetch https://supportportal.juniper.net/s/sitemap.xml: "
            "certificate verify failed"
        )

    monkeypatch.setattr(sources, "discover_juniper_security_sitemaps", _raise)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert "certificate verify failed" in entry["detail"]


def test_evaluate_juniper_security_unavailable_when_index_malformed(monkeypatch):
    # A malformed/off-host/removed-family index failure is discovered while
    # assembling the set of child sitemaps to read (inside the *fetch*
    # phase, alongside network failures), not while parsing an
    # already-fetched child body -- so it is reported the same as an
    # unreachable index, not as "changed".
    def _raise():
        raise sources.SourceFetchError(
            "Juniper sitemap index no longer lists any topic-article sitemap children"
        )

    monkeypatch.setattr(sources, "discover_juniper_security_sitemaps", _raise)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert "no longer lists any topic-article" in entry["detail"]


def test_evaluate_juniper_security_unavailable_on_child_fetch_failure(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_security_sitemaps",
        lambda: ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"],
    )

    def _raise(url):
        raise sources.SourceFetchError(f"failed to fetch {url}: 404 Not Found")

    monkeypatch.setattr(sources, "fetch_text", _raise)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert "404 Not Found" in entry["detail"]


def test_evaluate_juniper_security_changed_on_child_malformed_xml(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_security_sitemaps",
        lambda: ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"],
    )
    monkeypatch.setattr(sources, "fetch_text", lambda url: "not xml")

    def _raise(text):
        raise sources.SourceFetchError("invalid Juniper sitemap XML")

    monkeypatch.setattr(sources, "parse_juniper_security_sitemap", _raise)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "invalid Juniper sitemap XML" in entry["detail"]


def test_evaluate_juniper_security_changed_on_source_identity_mismatch(monkeypatch):
    monkeypatch.setattr(
        sources,
        "discover_juniper_security_sitemaps",
        lambda: ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"],
    )
    monkeypatch.setattr(sources, "fetch_text", lambda url: "<urlset></urlset>")
    monkeypatch.setattr(
        sources, "parse_juniper_security_sitemap", lambda text: {"https://example.test/bulletin"}
    )

    def _raise(family, actual_urls):
        raise provenance.SourceProvenanceError(f"{family} source URLs no longer match")

    monkeypatch.setattr(provenance, "validate_source_identity", _raise)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_CHANGED
    assert "no longer match" in entry["detail"]


def test_evaluate_juniper_security_successful_discovery_end_to_end(monkeypatch):
    # Exercises the real index parser and child parser together (only the
    # network layer is mocked) so the full sitemap-index -> child sitemaps
    # -> Mist/Apstra bulletin URLs chain is covered, not just its pieces.
    pages = {
        sources.JUNIPER_SECURITY_SITEMAP_INDEX_URL: (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topic-1.xml</loc></sitemap>"
            "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml</loc></sitemap>"
            "<sitemap><loc>https://supportportal.juniper.net/s/sitemap-view-1.xml</loc></sitemap>"
            "</sitemapindex>"
        ),
        "https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml": (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://supportportal.juniper.net/s/article/2026-Security-Bulletin-Apstra-CVE-1</loc></url>"
            "<url><loc>https://supportportal.juniper.net/s/article/not-a-bulletin</loc></url>"
            "</urlset>"
        ),
    }
    monkeypatch.setattr(sources, "fetch_text", lambda url: pages[url])

    def _validate_identity(family, urls):
        assert urls == [sources.JUNIPER_SECURITY_SITEMAP_INDEX_URL]

    monkeypatch.setattr(provenance, "validate_source_identity", _validate_identity)

    entry = drift.evaluate_juniper_security()

    assert entry["status"] == drift.STATUS_FRESH
    assert entry["count"] == 1


# ---------------------------------------------------------------------------
# Explicit coverage gap
# ---------------------------------------------------------------------------


def test_evaluate_hpe_aruba_coverage_gap_never_reports_fresh():
    entry = drift.evaluate_hpe_aruba_coverage_gap()

    assert entry["status"] == drift.STATUS_COVERAGE_GAP
    assert entry["status"] != drift.STATUS_FRESH
    assert entry["drift_detected"] is False
    assert entry["source"] == "hpe_aruba_current_lifecycle"
    assert len(entry["detail"]) <= contracts.MAX_FRESHNESS_DETAIL_CHARS


# ---------------------------------------------------------------------------
# check_sources() backward compatibility
# ---------------------------------------------------------------------------


def _patch_all_fresh(monkeypatch):
    monkeypatch.setattr(sources, "fetch_text", lambda url: "content")
    monkeypatch.setattr(sources, "fetch_bytes", lambda url: b"pdf")
    monkeypatch.setattr(
        sources, "parse_changes_csv", lambda text: [object()] * drift.MIN_ARUBA_ADVISORIES
    )
    monkeypatch.setattr(provenance, "validate_markers", lambda family, text: None)
    monkeypatch.setattr(
        sources,
        "extract_aruba_hardware_eos_text",
        lambda value: "hardware",
    )
    monkeypatch.setattr(
        sources,
        "parse_hpe_lifecycle_xml",
        lambda text: [object()] * drift.MIN_HPE_LIFECYCLE_NOTICES,
    )
    monkeypatch.setattr(
        sources,
        "discover_juniper_lifecycle_urls",
        lambda: dict(sources.JUNIPER_LIFECYCLE_URLS),
    )
    monkeypatch.setattr(sources, "render_juniper_lifecycle_page", lambda text, url: "rendered")
    monkeypatch.setattr(
        sources,
        "discover_juniper_security_sitemaps",
        lambda: ["https://supportportal.juniper.net/s/sitemap-topicarticle-1.xml"],
    )
    monkeypatch.setattr(
        sources, "parse_juniper_security_sitemap", lambda text: {"https://example.test/bulletin"}
    )
    monkeypatch.setattr(provenance, "validate_source_identity", lambda family, urls: None)


def test_check_sources_reports_official_source_counts(monkeypatch):
    _patch_all_fresh(monkeypatch)

    counts = drift.check_sources()

    assert counts["aruba_advisories"] == drift.MIN_ARUBA_ADVISORIES
    assert counts["hpe_lifecycle_notices"] == drift.MIN_HPE_LIFECYCLE_NOTICES
    assert counts["juniper_lifecycle_pages"] == len(sources.JUNIPER_LIFECYCLE_URLS)
    assert counts["juniper_security_bulletins"] == 1
    assert counts["hpe_aruba_current_lifecycle"] == 0


def test_check_sources_fails_when_coverage_drops(monkeypatch):
    _patch_all_fresh(monkeypatch)
    monkeypatch.setattr(sources, "parse_changes_csv", lambda text: [])

    with pytest.raises(SystemExit, match="coverage regressed"):
        drift.check_sources()


def test_check_sources_fails_when_source_unavailable(monkeypatch):
    _patch_all_fresh(monkeypatch)

    def _raise(url):
        raise sources.SourceFetchError(f"failed to fetch {url}: connection refused")

    monkeypatch.setattr(sources, "fetch_text", _raise)

    with pytest.raises(SystemExit, match="coverage regressed"):
        drift.check_sources()


def test_check_sources_does_not_fail_on_coverage_gap_alone(monkeypatch):
    # All real sources fresh; the coverage-gap entry alone must never
    # trigger a failing exit -- it is an already-reviewed, expected state.
    _patch_all_fresh(monkeypatch)

    counts = drift.check_sources()

    assert counts["hpe_aruba_current_lifecycle"] == 0


# ---------------------------------------------------------------------------
# Freshness artifact -- bounded, redacted, deterministic content
# ---------------------------------------------------------------------------


def test_build_freshness_artifact_writes_bounded_deterministic_entries(tmp_path):
    entries = [
        drift._entry("aruba_advisories", 99, 90, drift.STATUS_FRESH),
        drift._entry(
            "juniper_security_bulletins",
            0,
            1,
            drift.STATUS_UNAVAILABLE,
            "failed to fetch https://supportportal.juniper.net/x: connection refused",
        ),
        drift._entry(
            "hpe_aruba_current_lifecycle", 0, 0, drift.STATUS_COVERAGE_GAP, "documented gap"
        ),
    ]

    output_path = tmp_path / "source-freshness.json"
    manifest_entry = drift.build_freshness_artifact(entries, output_path=output_path)

    assert manifest_entry.kind == contracts.SOURCE_FRESHNESS_RESULT
    assert manifest_entry.redacted is True
    written = json.loads(output_path.read_text())
    assert written["kind"] == contracts.SOURCE_FRESHNESS_RESULT
    assert len(written["entries"]) == 3
    # Bounded: nothing exceeds the contract's per-detail character cap.
    for entry in written["entries"]:
        assert len(entry["detail"]) <= contracts.MAX_FRESHNESS_DETAIL_CHARS


def test_build_freshness_artifact_redacts_token_shaped_detail_defense_in_depth(tmp_path):
    # These sources never carry credentials in practice (no authenticated
    # fetch is ever made), but the artifact writer's shared redaction path
    # (hpe_networking_mcp.pipeline.artifact_contracts.redact_artifact_payload ->
    # hpe_networking_mcp.mcp_servers.shared.redact_sensitive) must still scrub a
    # credential-shaped detail string as defense in depth.
    entries = [
        drift._entry(
            "aruba_advisories",
            0,
            90,
            drift.STATUS_UNAVAILABLE,
            "bearer eyJhbGciOiJIUzI1NiJ9.leaked-token-should-not-persist",
        ),
    ]

    output_path = tmp_path / "source-freshness.json"
    drift.build_freshness_artifact(entries, output_path=output_path)

    raw_text = output_path.read_text()
    assert "leaked-token-should-not-persist" not in raw_text


def test_build_freshness_artifact_is_content_deterministic_except_timestamp(tmp_path):
    entries = [drift._entry("aruba_advisories", 99, 90, drift.STATUS_FRESH)]

    first = drift.build_freshness_artifact(entries, output_path=tmp_path / "a.json")
    second = drift.build_freshness_artifact(entries, output_path=tmp_path / "b.json")

    first_body = json.loads((tmp_path / "a.json").read_text())
    second_body = json.loads((tmp_path / "b.json").read_text())
    del first_body["generated_at"]
    del second_body["generated_at"]
    assert first_body == second_body
    assert first.sha256 != "" and second.sha256 != ""


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_all_fresh_or_coverage_gap(monkeypatch, tmp_path, capsys):
    _patch_all_fresh(monkeypatch)
    monkeypatch.setattr(
        drift.sys,
        "argv",
        [
            "prog",
            "--artifact-path",
            str(tmp_path / "freshness.json"),
            "--drift-artifact-path",
            str(tmp_path / "drift.json"),
        ],
    )

    exit_code = drift.main()

    assert exit_code == 0
    assert (tmp_path / "freshness.json").exists()


def test_main_exits_nonzero_when_a_source_regresses(monkeypatch, tmp_path):
    _patch_all_fresh(monkeypatch)
    monkeypatch.setattr(sources, "parse_changes_csv", lambda text: [])
    monkeypatch.setattr(
        drift.sys,
        "argv",
        [
            "prog",
            "--artifact-path",
            str(tmp_path / "freshness.json"),
            "--no-drift-artifact",
        ],
    )

    exit_code = drift.main()

    # A count regression is a confirmed coverage/content change, so it exits
    # with the classified content-drift code -- not the transport code.
    assert exit_code == taxonomy.EXIT_CONTENT_DRIFT


def test_main_legacy_exit_code_mode_still_exits_one(monkeypatch, tmp_path):
    _patch_all_fresh(monkeypatch)
    monkeypatch.setattr(sources, "parse_changes_csv", lambda text: [])
    monkeypatch.setattr(
        drift.sys,
        "argv",
        [
            "prog",
            "--artifact-path",
            str(tmp_path / "freshness.json"),
            "--no-drift-artifact",
            "--exit-code-mode",
            "legacy",
        ],
    )

    assert drift.main() == 1


def test_main_no_artifact_flag_skips_writing(monkeypatch, tmp_path):
    _patch_all_fresh(monkeypatch)
    sentinel_path = tmp_path / "should-not-be-created.json"
    monkeypatch.setattr(drift, "_DEFAULT_ARTIFACT_PATH", sentinel_path)
    monkeypatch.setattr(drift.sys, "argv", ["prog", "--no-artifact", "--no-drift-artifact"])

    exit_code = drift.main()

    assert exit_code == 0
    assert not sentinel_path.exists()


# ---------------------------------------------------------------------------
# Shared drift taxonomy mapping
# ---------------------------------------------------------------------------


def test_every_source_status_maps_to_a_taxonomy_class():
    assert set(drift._RESULT_CLASS_BY_STATUS) == {
        drift.STATUS_FRESH,
        drift.STATUS_STALE,
        drift.STATUS_UNAVAILABLE,
        drift.STATUS_CHANGED,
        drift.STATUS_COVERAGE_GAP,
    }
    for result_class in drift._RESULT_CLASS_BY_STATUS.values():
        assert result_class in taxonomy.RESULT_CLASSES


def test_fetch_failure_is_unavailable_not_content_drift():
    def _fetch():
        raise sources.SourceFetchError("connection reset")

    entry = drift._evaluate(source="s", minimum=1, fetch=_fetch, parse=lambda raw: raw)

    assert entry["status"] == drift.STATUS_UNAVAILABLE
    assert entry["result_class"] == taxonomy.UNAVAILABLE
    assert entry["result_class"] != taxonomy.CONTENT_DRIFT


def test_parse_failure_is_parser_error_not_pointer_change():
    def _parse(raw):
        raise sources.SourceFetchError("malformed sitemap body")

    entry = drift._evaluate(source="s", minimum=1, fetch=lambda: "raw", parse=_parse)

    assert entry["status"] == drift.STATUS_CHANGED
    assert entry["result_class"] == taxonomy.PARSER_ERROR


def test_provenance_mismatch_is_pointer_change():
    def _parse(raw):
        raise provenance.SourceProvenanceError("source URL set no longer matches the pin")

    entry = drift._evaluate(source="s", minimum=1, fetch=lambda: "raw", parse=_parse)

    assert entry["status"] == drift.STATUS_CHANGED
    assert entry["result_class"] == taxonomy.POINTER_CHANGE


def test_count_regression_is_content_drift():
    entry = drift._evaluate(
        source="s", minimum=5, fetch=lambda: "raw", parse=lambda raw: [1, 2]
    )

    assert entry["status"] == drift.STATUS_STALE
    assert entry["result_class"] == taxonomy.CONTENT_DRIFT


def test_coverage_gap_entry_is_never_fresh():
    entry = drift.evaluate_hpe_aruba_coverage_gap()

    assert entry["status"] == drift.STATUS_COVERAGE_GAP
    assert entry["result_class"] == taxonomy.COVERAGE_GAP
    assert entry["drift_detected"] is False


def test_drift_report_separates_incomplete_checks_from_drift():
    entries = [
        drift._entry("a", 0, 1, drift.STATUS_UNAVAILABLE, "network"),
        drift._entry("b", 3, 1, drift.STATUS_FRESH),
    ]

    report = drift.build_drift_report(entries)

    assert report["check"] == drift.CHECK_NAME
    assert report["content_drift_detected"] is False
    assert report["check_incomplete"] is True
    assert report["exit_code"] == taxonomy.EXIT_UNAVAILABLE


def test_freshness_artifact_stays_within_the_strict_contract(tmp_path):
    """result_class travels in the drift report, not in the strict, versioned
    source_freshness_result artifact -- the contract is not widened."""
    entries = [drift._entry("a", 3, 1, drift.STATUS_FRESH)]
    path = tmp_path / "freshness.json"

    drift.build_freshness_artifact(entries, output_path=path)

    payload = json.loads(path.read_text())
    assert "result_class" not in payload["entries"][0]
    assert payload["entries"][0]["status"] == drift.STATUS_FRESH
