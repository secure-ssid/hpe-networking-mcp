from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline.clients import lance_client, rag_diagnostics
from ingestion import ingest_docs


def _write_sources(sources_dir, family: str, files: dict[str, str]) -> None:
    folder = sources_dir / family
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text)


def test_ingestion_delta_reports_new_changed_removed_unchanged(tmp_path, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    # ingest_docs.collect_points computes file_path relative to its own
    # module-level SOURCES_DIR constant (not the sources_dir argument this
    # diagnostic passes as a directory to walk); point it at our fixture
    # root for the duration of this test.
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)

    _write_sources(
        sources_dir,
        "security_advisories",
        {"a.md": "advisory A body", "b.md": "advisory B body"},
    )

    # Seed the LanceDB docs table as if a prior full ingest already ran,
    # with one row matching "a.md" (unchanged), one stale row for a file
    # that no longer exists ("removed"), and nothing for "b.md" ("new").
    db = lance_client.connect(data_dir)
    a_records = ingest_docs.collect_points(sources_dir / "security_advisories", "security-advisory")
    assert len(a_records) == 2
    a_record = next(r for r in a_records if r["file_path"].endswith("a.md"))
    seed_rows = [
        {**a_record, "vector": [0.0, 0.0]},
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "text": "stale",
            "source": "security_advisories",
            "doc_type": "security-advisory",
            "file_path": "security_advisories/removed.md",
            "chunk_index": 0,
            "content_hash": "stale-hash",
            "vector": [0.0, 0.0],
        },
    ]
    lance_client.create_docs_table(db, seed_rows)

    result = rag_diagnostics.ingestion_delta(
        ("security_advisories",), sources_dir=sources_dir, data_dir=data_dir
    )

    entry = result["sources"]["security_advisories"]
    assert entry["status"] == "indexed"
    assert entry["new"] == 1  # b.md
    assert entry["removed"] == 1  # removed.md
    assert entry["unchanged"] == 1  # a.md, hash matches
    assert entry["changed"] == 0


def test_ingestion_delta_reports_not_yet_indexed_and_missing_dir(tmp_path, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)
    _write_sources(sources_dir, "lifecycle_notices", {"n.md": "notice body"})

    result = rag_diagnostics.ingestion_delta(
        ("lifecycle_notices", "juniper_lifecycle"),
        sources_dir=sources_dir,
        data_dir=data_dir,
    )

    assert result["sources"]["lifecycle_notices"]["status"] == "not_yet_indexed"
    assert result["sources"]["lifecycle_notices"]["new"] == 1
    assert result["sources"]["juniper_lifecycle"] == {
        "status": "missing_source_dir",
        "new": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
    }


def test_ingestion_delta_requires_full_rebuild_for_legacy_table(tmp_path, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)
    _write_sources(
        sources_dir,
        "security_advisories",
        {"a.md": "advisory body"},
    )
    db = lance_client.connect(data_dir)
    lance_client.create_docs_table(
        db,
        [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "text": "legacy",
                "source": "security_advisories",
                "doc_type": "security-advisory",
                "file_path": "security_advisories/a.md",
                "chunk_index": 0,
                "vector": [0.0, 0.0],
            }
        ],
    )

    result = rag_diagnostics.ingestion_delta(
        ("security_advisories",),
        sources_dir=sources_dir,
        data_dir=data_dir,
    )

    assert result["sources"]["security_advisories"] == {
        "status": "full_rebuild_required",
        "new": 1,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
    }


def test_ingestion_delta_rejects_unsupported_source_family(tmp_path):
    with pytest.raises(ValueError, match="unsupported source families"):
        rag_diagnostics.ingestion_delta(
            ("developer_docs",), sources_dir=tmp_path, data_dir=tmp_path
        )


def test_ingestion_delta_suppresses_collect_points_stdout(tmp_path, capsys, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)
    _write_sources(sources_dir, "security_advisories", {"a.md": "advisory body"})

    rag_diagnostics.ingestion_delta(
        ("security_advisories",), sources_dir=sources_dir, data_dir=data_dir
    )

    captured = capsys.readouterr()
    assert captured.out == ""


def _write_freshness_artifact(path, entries) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": entries,
    }
    contracts.write_artifact(path, contracts.SOURCE_FRESHNESS_RESULT, payload)


def test_freshness_summary_reduces_entries_to_status_counts(tmp_path):
    artifact_path = tmp_path / "source-freshness.json"
    _write_freshness_artifact(
        artifact_path,
        [
            {
                "source": "aruba_advisories",
                "count": 99,
                "minimum": 90,
                "status": "fresh",
                "drift_detected": False,
                "detail": "",
            },
            {
                "source": "hpe_lifecycle_notices",
                "count": 300,
                "minimum": 340,
                "status": "stale",
                "drift_detected": True,
                "detail": "count 300 below minimum 340",
            },
            {
                "source": "hpe_aruba_current_lifecycle",
                "count": 0,
                "minimum": 0,
                "status": "coverage_gap",
                "drift_detected": False,
                "detail": "no reliable machine-readable source",
            },
        ],
    )

    result = rag_diagnostics.freshness_summary(artifact_path)

    assert result["status_counts"] == {"fresh": 1, "stale": 1, "coverage_gap": 1}
    assert result["schema_version"] == 1
    assert len(result["entries"]) == 3
    assert {e["source"] for e in result["entries"]} == {
        "aruba_advisories",
        "hpe_lifecycle_notices",
        "hpe_aruba_current_lifecycle",
    }


def test_freshness_summary_missing_artifact_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="No source-freshness artifact"):
        rag_diagnostics.freshness_summary(tmp_path / "does-not-exist.json")


def test_freshness_summary_rejects_malformed_artifact(tmp_path):
    path = tmp_path / "source-freshness.json"
    path.write_text('{"generated_at": "not-a-timestamp", "entries": []}')

    with pytest.raises(contracts.ArtifactValidationError):
        rag_diagnostics.freshness_summary(path)


def test_full_corpus_delta_covers_every_known_vector_source_family(tmp_path, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)

    result = rag_diagnostics.full_corpus_delta(sources_dir=sources_dir, data_dir=data_dir)

    expected_families = {
        folder
        for folder in ingest_docs.SOURCE_META
        if not ingest_docs.source_uses_structured_index(folder, "lancedb")
    }
    assert set(result["sources"]) == expected_families
    assert "openapi_specs" not in result["sources"]
    # None of the fixture directories exist yet -- every family should
    # report the same bounded "missing_source_dir" shape as ingestion_delta.
    for entry in result["sources"].values():
        assert entry == {
            "status": "missing_source_dir",
            "new": 0,
            "changed": 0,
            "removed": 0,
            "unchanged": 0,
        }


def test_full_corpus_delta_diffs_a_non_security_lifecycle_family(tmp_path, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)

    # "devhub" is outside ingestion_delta's four security/lifecycle
    # families, exercising exactly the corpus-wide generalization.
    _write_sources(sources_dir, "devhub", {"guide.md": "devhub guide body"})

    result = rag_diagnostics.full_corpus_delta(sources_dir=sources_dir, data_dir=data_dir)

    entry = result["sources"]["devhub"]
    assert entry["status"] == "not_yet_indexed"
    assert entry["new"] == 1
    assert entry["removed"] == 0


def test_full_corpus_delta_suppresses_collect_points_stdout(tmp_path, capsys, monkeypatch):
    sources_dir = tmp_path / "sources"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest_docs, "SOURCES_DIR", sources_dir)
    _write_sources(sources_dir, "devhub", {"guide.md": "devhub guide body"})

    rag_diagnostics.full_corpus_delta(sources_dir=sources_dir, data_dir=data_dir)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_ingestion_delta_works_when_repo_root_is_not_on_sys_path(tmp_path, monkeypatch):
    """Regression guard for the fragile ``from ingestion import ingest_docs``.

    The real MCP router is launched with ``PYTHONPATH=<repo>/src`` only (see
    ``.cursor/mcp.dev.json``); under that launch, ``sys.path[0]`` is the
    script's own directory, not the repo root, so a bare
    ``from ingestion import ingest_docs`` raises ``ModuleNotFoundError``
    (reproduced directly before this fix, both standalone and through this
    exact call). Pytest's own rootdir insertion normally hides this because
    the repo root is already on ``sys.path`` (and ``ingestion.ingest_docs``
    already cached in ``sys.modules``) for every other test in this file --
    so this test evicts both to prove ``_ensure_ingestion_importable()``
    (not ambient test-runner convenience) is what makes the import succeed.

    Uses the real, on-disk ``ingestion/sources`` tree rather than a fixture
    directory: ``ingest_docs.SOURCES_DIR`` is a module-level constant on
    whatever module object ends up cached in ``sys.modules`` after the
    eviction below, so monkeypatching it on the pre-eviction module object
    would silently not apply to the fresh one this test deliberately forces.
    The four security/lifecycle raw-source folders are gitignored/regenerable
    (see ``ingestion/sources/`` in the repo layout docs) and reliably absent
    in a plain checkout, so every family deterministically reports
    ``missing_source_dir`` here regardless of scrape state.
    """
    data_dir = tmp_path / "data"

    repo_root_str = str(rag_diagnostics.ROOT)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in (repo_root_str, "")])
    for name in ("ingestion", "ingestion.ingest_docs", "ingestion.chunking"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    result = rag_diagnostics.ingestion_delta(data_dir=data_dir)

    assert set(result["sources"]) == set(rag_diagnostics.DELTA_SOURCE_FAMILIES)
    for entry in result["sources"].values():
        assert entry["status"] == "missing_source_dir"
