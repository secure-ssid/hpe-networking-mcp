"""Tests for prose-ingestion provenance and fail-closed source handling."""

from __future__ import annotations

from ingestion import ingest_docs
from ingestion.chunking import (
    breadcrumb_at,
    chunk_text,
    chunk_text_with_breadcrumbs,
    heading_breadcrumbs,
)


def test_extract_source_url_reads_scraper_marker():
    text = "<!-- source: https://example.com/guide -->\n\n# Guide\n\nBody."

    assert ingest_docs.extract_source_url(text) == "https://example.com/guide"


def test_extract_source_url_ignores_deep_body_comment():
    text = "x" * 600 + "<!-- source: https://example.com/decoy -->"

    assert ingest_docs.extract_source_url(text) is None


def test_html_source_url_is_recovered_from_raw_file(tmp_path):
    path = tmp_path / "guide.html"
    path.write_text(
        "<!-- source: https://example.com/html-guide --><main><p>Guide content.</p></main>",
        encoding="utf-8",
    )

    converted = ingest_docs.html_to_text(path.read_text(encoding="utf-8"))

    assert ingest_docs._file_source_url(path, converted) == ("https://example.com/html-guide")


def test_heading_breadcrumbs_track_nested_sections():
    text = "# Top\n\n## WLAN\n\n### Security\n\n## Switching"
    breadcrumbs = heading_breadcrumbs(text)

    assert breadcrumb_at(breadcrumbs, text.index("Security")) == "Top > WLAN > Security"
    assert breadcrumb_at(breadcrumbs, text.index("Switching")) == "Top > Switching"


def test_chunk_text_with_breadcrumbs_preserves_chunk_boundaries():
    text = "# Top\n\n" + "intro " * 80 + "\n\n## WLAN\n\n" + "security " * 180

    pairs = chunk_text_with_breadcrumbs(text)

    assert [chunk for chunk, _ in pairs] == chunk_text(text)
    assert any(breadcrumb == "Top > WLAN" for _, breadcrumb in pairs)


def test_collect_points_emits_provenance_metadata(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    source_dir = sources / "sample_docs"
    source_dir.mkdir(parents=True)
    (source_dir / "guide.md").write_text(
        "<!-- source: https://example.com/guide -->\n\n"
        "# Guide\n\n" + "Useful configuration guidance. " * 20,
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources)

    records = ingest_docs.collect_points(source_dir, "guide")

    assert records
    assert all(record["source_url"] == "https://example.com/guide" for record in records)
    assert any(record["heading_breadcrumb"] == "Guide" for record in records)


def test_missing_required_sources_reports_absent_folders(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "security_advisories").mkdir()

    missing = ingest_docs.missing_required_sources(sources)

    assert "security_advisories" not in missing
    assert "lifecycle_notices" in missing
    assert "juniper_lifecycle" in missing
    assert "juniper_security_advisories" in missing
