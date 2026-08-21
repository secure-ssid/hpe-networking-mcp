from __future__ import annotations

import json

from ingestion import ingest_docs


def test_derive_metadata_is_deterministic_for_aoscx_release_path():
    args = (
        "aoscx_release_notes",
        "aoscx_release_notes/8325/10-13-0005/overview.html",
        "https://support.hpe.com/docs/aoscx",
    )

    first = ingest_docs.derive_metadata(*args)
    second = ingest_docs.derive_rag_metadata(*args)

    assert first == second == {
        "vendor": "aruba",
        "product": "aos-cx",
        "platform": "switch",
        "model": "8325",
        "release": "10.13",
        "version": "10.13.0005",
        "document_family": "release-notes",
        "record_type": "document",
        "authority": "official_vendor",
        "freshness": None,
    }


def test_derive_metadata_parses_junos_and_datasheet_identity():
    junos = ingest_docs.derive_metadata(
        "junos_ex_release_notes",
        "junos_ex_release_notes/24_2r1/ex-resolved-issues-cover.html",
        "https://www.juniper.net/documentation/us/en/software/junos/release-notes/24.2/",
    )
    datasheet = ingest_docs.derive_metadata(
        "product_datasheets",
        "product_datasheets/switch-ex4100-f-ethernet-switch.md",
        "https://www.juniper.net/us/en/products/switches/ex-series/ex4100-f-ethernet-switch/specs.html",
    )

    assert (junos["vendor"], junos["product"], junos["platform"]) == (
        "juniper",
        "junos",
        "ex",
    )
    assert (junos["release"], junos["version"]) == ("24.2", "24.2R1")
    assert (datasheet["product"], datasheet["platform"], datasheet["model"]) == (
        "ex-series",
        "switch",
        "EX4100-F",
    )


def test_derive_metadata_uses_provenance_for_authority_and_freshness():
    metadata = ingest_docs.derive_metadata(
        "unknown_source",
        "unknown/page.md",
        provenance={
            "authority": "official_vendor_openapi",
            "reviewed_at": "2026-08-01",
        },
    )

    assert metadata["authority"] == "official_vendor_openapi"
    assert metadata["freshness"] == "2026-08-01"
    assert metadata["vendor"] is None
    assert metadata["product"] is None


def test_collect_points_adds_metadata_to_prose_rows(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    source_dir = sources / "aoscx_release_notes"
    source_dir.mkdir(parents=True)
    path = source_dir / "8325" / "10-13-0005" / "overview.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "<!-- source: https://support.hpe.com/docs/aoscx -->\n\n"
        "# Overview\n\n"
        + ("A release note body with enough content to index. " * 20),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources)

    records = ingest_docs.collect_points(source_dir, "aoscx-release-notes")

    assert records
    assert records[0]["id"] == ingest_docs.stable_id(
        "aoscx_release_notes/8325/10-13-0005/overview.md", 0
    )
    assert records[0]["vendor"] == "aruba"
    assert records[0]["product"] == "aos-cx"
    assert records[0]["model"] == "8325"
    assert records[0]["version"] == "10.13.0005"
    assert records[0]["record_type"] == "document"


def test_collect_openapi_points_adds_metadata_without_changing_ids(
    tmp_path, monkeypatch
):
    sources = tmp_path / "sources"
    source_dir = sources / "product_specs"
    source_dir.mkdir(parents=True)
    (source_dir / "cppm-api.json").write_text(
        json.dumps(
            {
                "info": {"title": "ClearPass API", "version": "6.12"},
                "components": {
                    "schemas": {
                        "Thing": {
                            "properties": {"id": {"type": "string"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources)

    records = ingest_docs.collect_openapi_points(source_dir, "product-openapi")

    assert len(records) == 1
    assert records[0]["id"] == ingest_docs._md5_uuid(
        "product_specs/cppm-api.json:schema:Thing"
    )
    assert records[0]["record_type"] == "schema"
    assert records[0]["document_family"] == "api-reference"
    assert records[0]["product"] == "clearpass"
    assert records[0]["version"] == "6.12"
