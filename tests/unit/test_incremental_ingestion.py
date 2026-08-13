from __future__ import annotations

from ingestion import ingest_docs
from hpe_networking_mcp.pipeline.clients import lance_client


def _row(doc_id: str, text: str, content_hash: str) -> dict:
    return {
        "id": doc_id,
        "text": text,
        "source": "docs",
        "doc_type": "guide",
        "file_path": "docs/example.md",
        "chunk_index": 0,
        "content_hash": content_hash,
        "vector": [0.0, 0.0],
    }


def test_content_hash_is_stable_and_changes_with_text():
    assert ingest_docs.content_hash("same") == ingest_docs.content_hash("same")
    assert ingest_docs.content_hash("same") != ingest_docs.content_hash("changed")


def test_lancedb_merge_and_delete_support_incremental_rows(tmp_path):
    db = lance_client.connect(tmp_path)
    first_id = "11111111-1111-1111-1111-111111111111"
    second_id = "22222222-2222-2222-2222-222222222222"
    lance_client.create_docs_table(db, [_row(first_id, "old", "old")])

    lance_client.merge_docs_rows(
        db,
        [
            _row(first_id, "updated", "new"),
            _row(second_id, "inserted", "inserted"),
        ],
    )

    metadata = {row["id"]: row for row in lance_client.docs_metadata(db)}
    assert metadata[first_id]["content_hash"] == "new"
    assert second_id in metadata

    lance_client.delete_docs_ids(db, [first_id])

    assert lance_client.doc_count(db) == 1
    assert lance_client.docs_metadata(db)[0]["id"] == second_id


def test_invalid_incremental_delete_id_is_rejected(tmp_path):
    db = lance_client.connect(tmp_path)
    lance_client.create_docs_table(
        db,
        [_row("11111111-1111-1111-1111-111111111111", "old", "old")],
    )

    try:
        lance_client.delete_docs_ids(db, ["not-a-safe-id"])
    except ValueError as exc:
        assert "invalid document id" in str(exc)
    else:
        raise AssertionError("invalid ID was accepted")


def test_incremental_upload_deletes_chunks_for_removed_source_directory(
    tmp_path, monkeypatch
):
    """A full-corpus incremental run must sweep up stale chunks left behind
    when an entire known source directory is deleted or renamed on disk,
    even though nothing in the current run names that source explicitly."""
    db = lance_client.connect(tmp_path)
    kept_id = "33333333-3333-3333-3333-333333333333"
    removed_id = "44444444-4444-4444-4444-444444444444"
    kept_row = {**_row(kept_id, "kept text", "kept-hash"), "source": "kept_source"}
    removed_row = {
        **_row(removed_id, "removed text", "removed-hash"),
        "source": "removed_source",
    }
    lance_client.create_docs_table(db, [kept_row, removed_row])

    # upload_lancedb_incremental always reconnects internally; point that at
    # the same in-memory/tmp_path table instead of the default data/ dir.
    monkeypatch.setattr(lance_client, "connect", lambda *a, **kw: db)

    # Simulate a full-corpus rerun after "removed_source"'s directory was
    # deleted (or renamed) on disk: only "kept_source" produces records now.
    current_record = {
        "id": kept_id,
        "text": "kept text",
        "source": "kept_source",
        "doc_type": "guide",
        "file_path": "docs/example.md",
        "chunk_index": 0,
        "content_hash": "kept-hash",
    }

    result = ingest_docs.upload_lancedb_incremental([current_record], ["kept_source"])

    assert result is True
    remaining_ids = {row["id"] for row in lance_client.docs_metadata(db)}
    assert remaining_ids == {kept_id}


def _source_record(doc_id: str, source: str, content_hash: str) -> dict:
    return {
        "id": doc_id,
        "text": "text",
        "source": source,
        "doc_type": "guide",
        "file_path": f"{source}/x.md",
        "chunk_index": 0,
        "content_hash": content_hash,
    }


def test_incremental_preserves_rows_for_known_source_absent_this_run(
    tmp_path, monkeypatch
):
    """A SOURCE_META source whose directory is simply not present this run (a
    partial local checkout) must have its indexed rows preserved, not swept —
    only a run that actually walked the source may delete from it."""
    db = lance_client.connect(tmp_path)
    present_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    absent_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    present_row = {
        **_row(present_id, "present", "present-hash"),
        "source": "developer_docs",
    }
    absent_row = {**_row(absent_id, "absent", "absent-hash"), "source": "tech_docs"}
    lance_client.create_docs_table(db, [present_row, absent_row])
    monkeypatch.setattr(lance_client, "connect", lambda *a, **kw: db)

    # Only developer_docs was walked; tech_docs' directory was absent (SKIPped),
    # so it contributes no records and is not in ingested_sources. Its rows
    # must survive.
    present_record = _source_record(present_id, "developer_docs", "present-hash")

    result = ingest_docs.upload_lancedb_incremental(
        [present_record], ["developer_docs"]
    )

    assert result is True
    remaining = {row["id"] for row in lance_client.docs_metadata(db)}
    assert remaining == {present_id, absent_id}


def test_incremental_sweeps_retired_source_but_preserves_known_absent(
    tmp_path, monkeypatch
):
    """Only a source no longer in SOURCE_META (truly retired) is swept when
    nothing this run names it; a known-but-absent SOURCE_META source is
    preserved in the same run."""
    db = lance_client.connect(tmp_path)
    present_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    retired_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    absent_known_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    rows = [
        {**_row(present_id, "p", "p-hash"), "source": "developer_docs"},
        {**_row(retired_id, "r", "r-hash"), "source": "old_retired_source"},
        {**_row(absent_known_id, "a", "a-hash"), "source": "tech_docs"},
    ]
    lance_client.create_docs_table(db, rows)
    monkeypatch.setattr(lance_client, "connect", lambda *a, **kw: db)

    present_record = _source_record(present_id, "developer_docs", "p-hash")

    result = ingest_docs.upload_lancedb_incremental(
        [present_record], ["developer_docs"]
    )

    assert result is True
    remaining = {row["id"] for row in lance_client.docs_metadata(db)}
    # retired source's row swept; known-but-absent source's row preserved.
    assert remaining == {present_id, absent_known_id}


def test_incremental_zero_records_fails_closed(tmp_path, monkeypatch):
    """A run that collected zero records must refuse to delete anything — a
    missing sources tree must never wipe the whole index."""
    import pytest

    db = lance_client.connect(tmp_path)
    doc_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    lance_client.create_docs_table(
        db, [{**_row(doc_id, "keep", "keep-hash"), "source": "tech_docs"}]
    )
    monkeypatch.setattr(lance_client, "connect", lambda *a, **kw: db)

    with pytest.raises(SystemExit):
        ingest_docs.upload_lancedb_incremental([], [])

    remaining = {row["id"] for row in lance_client.docs_metadata(db)}
    assert remaining == {doc_id}
