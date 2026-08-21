from __future__ import annotations

import json
import sqlite3
import sys
import tarfile

import pytest

from scripts import package_indexes


def _write_index_inputs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "docs.lance").mkdir(parents=True)
    (data_dir / "docs.lance" / "part.bin").write_text("docs")
    (data_dir / "tools.lance").mkdir()
    (data_dir / "tools.lance" / "part.bin").write_text("tools")
    with sqlite3.connect(data_dir / "specs.sqlite") as conn:
        conn.execute("CREATE TABLE endpoints (id TEXT)")
        conn.execute("CREATE TABLE schemas (id TEXT)")
        conn.execute("CREATE TABLE fields (id TEXT)")
        conn.execute("CREATE TABLE advisories (id TEXT)")
        conn.execute("CREATE TABLE lifecycle_events (id TEXT)")

    source_manifest = tmp_path / "source_manifest.json"
    source_manifest.write_text(json.dumps([{"source": "docs"}]) + "\n")
    monkeypatch.setattr(package_indexes, "DATA_DIR", data_dir)
    monkeypatch.setattr(package_indexes, "SOURCE_MANIFEST", source_manifest)


def test_source_manifest_summary_tracks_rag_sources():
    summary = package_indexes._source_manifest_summary()

    assert summary["path"] == "ingestion/source_manifest.json"
    assert len(summary["sha256"]) == 64
    assert summary["source_count"] == len(summary["sources"])
    assert "techdocs_html" in summary["sources"]
    assert "feature_navigator" in summary["sources"]


def test_artifact_manifest_includes_source_manifest():
    manifest = package_indexes._artifact_manifest("vtest")

    assert manifest["source_manifest"]["path"] == "ingestion/source_manifest.json"
    assert "openapi_specs" in manifest["source_manifest"]["sources"]


def test_package_indexes_embeds_source_manifest(tmp_path, monkeypatch):
    _write_index_inputs(tmp_path, monkeypatch)

    output_dir = tmp_path / "dist"
    archive, _ = package_indexes.package_indexes("vtest", output_dir)
    latest_archive, latest_checksum = package_indexes.write_latest_alias(archive, output_dir)

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        assert "data/SOURCE-MANIFEST.json" in names
        assert "data/INDEX-MANIFEST.json" in names
        source_data = json.load(tar.extractfile("data/SOURCE-MANIFEST.json"))
        index_data = json.load(tar.extractfile("data/INDEX-MANIFEST.json"))

    assert source_data == [{"source": "docs"}]
    assert index_data["source_manifest"]["sources"] == ["docs"]
    assert index_data["artifacts"]["specs.sqlite"]["counts"] == {
        "endpoints": 0,
        "schemas": 0,
        "fields": 0,
        "advisories": 0,
        "lifecycle_events": 0,
    }
    assert latest_archive.name == "hpe-networking-mcp-rag-index-latest.tar.gz"
    assert latest_archive.read_bytes() == archive.read_bytes()
    assert latest_checksum.read_text().endswith("  hpe-networking-mcp-rag-index-latest.tar.gz\n")


def test_main_can_skip_latest_alias(tmp_path, monkeypatch):
    _write_index_inputs(tmp_path, monkeypatch)
    output_dir = tmp_path / "dist"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package_indexes.py",
            "--version",
            "vtest",
            "--output-dir",
            str(output_dir),
            "--skip-latest-copy",
        ],
    )

    assert package_indexes.main() == 0
    assert (output_dir / "hpe-networking-mcp-rag-index-vtest.tar.gz").exists()
    assert (output_dir / "hpe-networking-mcp-rag-index-vtest.tar.gz.sha256").exists()
    assert not (output_dir / "hpe-networking-mcp-rag-index-latest.tar.gz").exists()
    assert not (output_dir / "hpe-networking-mcp-rag-index-latest.tar.gz.sha256").exists()


def _local_manifest_env(tmp_path, monkeypatch):
    """Point the local-manifest helpers at an isolated data/ + source manifest."""
    data_dir = tmp_path / "data"
    (data_dir / "docs.lance").mkdir(parents=True)
    (data_dir / "docs.lance" / "part.bin").write_text("docs")
    (data_dir / "tools.lance").mkdir()
    (data_dir / "tools.lance" / "part.bin").write_text("tools")
    with sqlite3.connect(data_dir / "specs.sqlite") as conn:
        conn.execute("CREATE TABLE endpoints (id TEXT)")
        conn.execute("INSERT INTO endpoints VALUES ('a')")

    source_manifest = tmp_path / "source_manifest.json"
    source_manifest.write_text(
        json.dumps([{"source": "devhub"}, {"source": "mist_docs"}], indent=2) + "\n"
    )
    monkeypatch.setattr(package_indexes, "DATA_DIR", data_dir)
    monkeypatch.setattr(package_indexes, "SOURCE_MANIFEST", source_manifest)
    monkeypatch.setattr(package_indexes, "LOCAL_SOURCE_MANIFEST", data_dir / "SOURCE-MANIFEST.json")
    monkeypatch.setattr(package_indexes, "LOCAL_INDEX_MANIFEST", data_dir / "INDEX-MANIFEST.json")
    return data_dir, source_manifest


def test_write_local_manifests_copies_declared_sources_verbatim(tmp_path, monkeypatch):
    data_dir, source_manifest = _local_manifest_env(tmp_path, monkeypatch)

    source_path, index_path = package_indexes.write_local_manifests("vtest")

    assert source_path.read_bytes() == source_manifest.read_bytes()
    index = json.loads(index_path.read_text())
    assert index["schema_version"] == package_indexes.INDEX_MANIFEST_SCHEMA_VERSION
    assert index["source_manifest"]["sources"] == ["devhub", "mist_docs"]
    assert index["artifacts"]["specs.sqlite"]["counts"]["endpoints"] == 1
    assert set(index["artifacts"]) == set(package_indexes.REQUIRED_ARTIFACTS)


def test_generated_index_manifest_never_implies_a_source_refresh(tmp_path, monkeypatch):
    """A reconciled manifest must not read as evidence of re-scraped sources."""
    _local_manifest_env(tmp_path, monkeypatch)

    _, index_path = package_indexes.write_local_manifests("vtest")
    index = json.loads(index_path.read_text())

    assert index["provenance"]["source_refresh_performed"] is False
    for name in package_indexes.REQUIRED_ARTIFACTS:
        assert index["artifacts"][name]["modified_at"]
        assert index["artifacts"][name]["sha256"]


def test_check_local_manifests_passes_after_reconciliation(tmp_path, monkeypatch):
    _local_manifest_env(tmp_path, monkeypatch)
    package_indexes.write_local_manifests("vtest")

    assert package_indexes.check_local_manifests() == []


def test_check_local_manifests_detects_a_stale_source_snapshot(tmp_path, monkeypatch):
    """The exact 9-source vs 16-source drift this gate exists to prevent."""
    data_dir, _ = _local_manifest_env(tmp_path, monkeypatch)
    package_indexes.write_local_manifests("vtest")
    stale = json.dumps([{"source": "devhub"}], indent=2) + "\n"
    (data_dir / "SOURCE-MANIFEST.json").write_text(stale)

    problems = package_indexes.check_local_manifests()

    assert any("SOURCE-MANIFEST.json" in problem for problem in problems)
    assert any("mist_docs" in problem for problem in problems)


def test_check_local_manifests_detects_a_rebuilt_index(tmp_path, monkeypatch):
    """A rebuilt artifact with an unchanged manifest is stale provenance."""
    data_dir, _ = _local_manifest_env(tmp_path, monkeypatch)
    package_indexes.write_local_manifests("vtest")
    (data_dir / "tools.lance" / "part.bin").write_text("rebuilt tools index")

    problems = package_indexes.check_local_manifests()

    assert any("tools.lance" in problem for problem in problems)


def test_check_local_manifests_detects_changed_specs_counts(tmp_path, monkeypatch):
    data_dir, _ = _local_manifest_env(tmp_path, monkeypatch)
    package_indexes.write_local_manifests("vtest")
    with sqlite3.connect(data_dir / "specs.sqlite") as conn:
        conn.execute("INSERT INTO endpoints VALUES ('b')")

    problems = package_indexes.check_local_manifests()

    assert any("specs.sqlite" in problem for problem in problems)


def test_check_local_manifests_flags_missing_manifests(tmp_path, monkeypatch):
    _local_manifest_env(tmp_path, monkeypatch)

    problems = package_indexes.check_local_manifests()

    assert any("SOURCE-MANIFEST.json" in problem for problem in problems)
    assert any("INDEX-MANIFEST.json" in problem for problem in problems)


def test_check_local_manifests_tolerates_a_no_data_checkout(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source_manifest = tmp_path / "source_manifest.json"
    source_manifest.write_text(json.dumps([{"source": "devhub"}]) + "\n")
    monkeypatch.setattr(package_indexes, "DATA_DIR", data_dir)
    monkeypatch.setattr(package_indexes, "SOURCE_MANIFEST", source_manifest)
    monkeypatch.setattr(package_indexes, "LOCAL_SOURCE_MANIFEST", data_dir / "SOURCE-MANIFEST.json")
    monkeypatch.setattr(package_indexes, "LOCAL_INDEX_MANIFEST", data_dir / "INDEX-MANIFEST.json")

    strict = package_indexes.check_local_manifests(require_artifacts=True)
    lenient = package_indexes.check_local_manifests(require_artifacts=False)

    assert any("missing index artifacts" in problem for problem in strict)
    assert lenient == []


def _real_doc_row(doc_id: str, source: str) -> dict:
    return {
        "id": doc_id,
        "text": f"Content for {source}",
        "source": source,
        "doc_type": source,
        "file_path": f"{source}/x.md",
        "chunk_index": 0,
        "vector": [0.0, 0.0],
    }


def _write_index_inputs_with_docs_rows(tmp_path, monkeypatch, doc_rows, declared_sources):
    """Like ``_write_index_inputs``, but backs ``data/docs.lance`` with a real
    LanceDB table so ``_index_contents``' per-source chunk counts are real,
    not the "no LanceDB table" fallback.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db = lance_client.connect(data_dir)
    lance_client.create_docs_table(db, doc_rows)
    (data_dir / "tools.lance").mkdir()
    (data_dir / "tools.lance" / "part.bin").write_text("tools")
    with sqlite3.connect(data_dir / "specs.sqlite") as conn:
        conn.execute("CREATE TABLE endpoints (id TEXT)")

    sources_root = tmp_path / "ingestion_sources"
    sources_root.mkdir()
    source_manifest = tmp_path / "source_manifest.json"
    source_manifest.write_text(
        json.dumps([{"source": s} for s in declared_sources], indent=2) + "\n"
    )
    monkeypatch.setattr(package_indexes, "DATA_DIR", data_dir)
    monkeypatch.setattr(package_indexes, "SOURCE_MANIFEST", source_manifest)
    monkeypatch.setattr(package_indexes, "SOURCES_ROOT", sources_root)
    monkeypatch.setattr(package_indexes, "LOCAL_SOURCE_MANIFEST", data_dir / "SOURCE-MANIFEST.json")
    monkeypatch.setattr(package_indexes, "LOCAL_INDEX_MANIFEST", data_dir / "INDEX-MANIFEST.json")
    return data_dir, sources_root


_REQUIRED_DECLARED = [
    "security_advisories",
    "lifecycle_notices",
    "juniper_lifecycle",
    "juniper_security_advisories",
]


def test_artifact_manifest_records_per_source_required_status_and_counts(tmp_path, monkeypatch):
    rows = [
        _real_doc_row("11111111-1111-1111-1111-111111111111", "security_advisories"),
        _real_doc_row("22222222-2222-2222-2222-222222222222", "lifecycle_notices"),
        _real_doc_row("33333333-3333-3333-3333-333333333333", "juniper_lifecycle"),
        _real_doc_row("44444444-4444-4444-4444-444444444444", "devhub"),
        # juniper_security_advisories intentionally has zero rows, no folder.
    ]
    data_dir, sources_root = _write_index_inputs_with_docs_rows(
        tmp_path, monkeypatch, rows, [*_REQUIRED_DECLARED, "devhub"]
    )
    (sources_root / "devhub").mkdir()
    (sources_root / "devhub" / "page.md").write_text("devhub content", encoding="utf-8")

    sources = package_indexes._artifact_manifest("vtest")["sources"]

    assert sources["security_advisories"]["required"] is True
    assert sources["devhub"]["required"] is False
    assert sources["security_advisories"]["indexed_chunk_count"] == 1
    assert sources["juniper_security_advisories"]["indexed_chunk_count"] == 0
    assert sources["juniper_security_advisories"]["present"] is False
    assert sources["juniper_security_advisories"]["sha256"] is None
    assert sources["devhub"]["present"] is True
    assert sources["devhub"]["sha256"] is not None
    assert sources["devhub"]["last_refreshed_at"]


def test_artifact_manifest_source_restored_updates_presence_and_digest(tmp_path, monkeypatch):
    """A source folder that reappears after being absent is reported present
    again with a fresh digest/timestamp -- the 'restored source' contract."""
    rows = [_real_doc_row("55555555-5555-5555-5555-555555555555", "devhub")]
    _, sources_root = _write_index_inputs_with_docs_rows(tmp_path, monkeypatch, rows, ["devhub"])

    before = package_indexes._artifact_manifest("vtest")["sources"]["devhub"]
    assert before["present"] is False

    (sources_root / "devhub").mkdir()
    (sources_root / "devhub" / "page.md").write_text("restored content", encoding="utf-8")

    after = package_indexes._artifact_manifest("vtest")["sources"]["devhub"]
    assert after["present"] is True
    assert after["file_count"] == 1
    assert after["sha256"] is not None


def test_package_indexes_refuses_when_required_source_has_zero_indexed_chunks(
    tmp_path, monkeypatch
):
    rows = [_real_doc_row("66666666-6666-6666-6666-666666666666", "devhub")]
    _write_index_inputs_with_docs_rows(tmp_path, monkeypatch, rows, [*_REQUIRED_DECLARED, "devhub"])

    with pytest.raises(SystemExit, match="required source"):
        package_indexes.package_indexes("vtest", tmp_path / "dist")


def test_package_indexes_succeeds_when_required_sources_are_indexed(tmp_path, monkeypatch):
    rows = [
        _real_doc_row("10000000-0000-0000-0000-000000000000", "security_advisories"),
        _real_doc_row("20000000-0000-0000-0000-000000000000", "lifecycle_notices"),
        _real_doc_row("30000000-0000-0000-0000-000000000000", "juniper_lifecycle"),
        _real_doc_row("40000000-0000-0000-0000-000000000000", "juniper_security_advisories"),
    ]
    _write_index_inputs_with_docs_rows(tmp_path, monkeypatch, rows, _REQUIRED_DECLARED)

    archive, checksum = package_indexes.package_indexes("vtest", tmp_path / "dist")

    assert archive.exists()
    assert checksum.exists()


def test_check_local_manifests_flags_required_source_with_zero_indexed_chunks(
    tmp_path, monkeypatch
):
    rows = [_real_doc_row("77777777-7777-7777-7777-777777777777", "devhub")]
    _write_index_inputs_with_docs_rows(tmp_path, monkeypatch, rows, [*_REQUIRED_DECLARED, "devhub"])
    package_indexes.write_local_manifests("vtest")

    problems = package_indexes.check_local_manifests()

    assert any("required source" in problem for problem in problems)
    assert any("security_advisories" in problem for problem in problems)


def test_committed_local_manifest_pair_matches_declared_sources():
    """The real data/ pair, when present, must describe all declared sources.

    Skipped on a no-data checkout: data/ is git-ignored, so CI without a
    restored bundle has nothing to reconcile. Strict release validation runs
    the same check with artifacts required.
    """
    if not (package_indexes.DATA_DIR / "docs.lance").exists():
        pytest.skip("no local data/docs.lance to reconcile")
    assert package_indexes.check_local_manifests() == []
