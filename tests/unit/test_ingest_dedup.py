"""Tests for dedup_records() ingestion-time deduplication."""
import hashlib

from ingestion import ingest_docs


def _rec(source: str, file_path: str, text: str = "SAME CONTENT", chunk_index: int = 0) -> dict:
    ch = hashlib.sha256(text.encode()).hexdigest()[:16]
    return {
        "id": f"{file_path}_{chunk_index}",
        "text": text,
        "source": source,
        "file_path": file_path,
        "chunk_index": chunk_index,
        "content_hash": ch,
    }


class TestDedupRecords:
    def test_unique_records_unchanged(self):
        recs = [
            _rec("docs", "a.html", text="TEXT A"),
            _rec("docs", "b.html", text="TEXT B"),
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert n_dropped == 0
        assert len(deduped) == 2

    def test_duplicates_collapsed_to_one(self):
        recs = [
            _rec("aoscx_guides", "a.html"),
            _rec("aoscx_guides", "b.html"),
            _rec("aoscx_guides", "c.html"),
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 1
        assert n_dropped == 2

    def test_high_authority_wins(self):
        """developer_docs (90) beats juniper_kb (30) for the same content."""
        recs = [
            _rec("juniper_kb", "a.html"),
            _rec("developer_docs", "b.html"),
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 1
        assert deduped[0]["source"] == "developer_docs"

    def test_tie_broken_by_file_path(self):
        """Same source priority: lexicographically earlier file_path wins."""
        recs = [
            _rec("aoscx_guides", "z/file.html"),
            _rec("aoscx_guides", "a/file.html"),
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 1
        assert deduped[0]["file_path"] == "a/file.html"

    def test_records_without_content_hash_kept(self):
        """Legacy records without content_hash must never be dropped."""
        recs = [
            {"id": "x", "text": "TEXT", "source": "docs", "file_path": "x.html"},
            {"id": "y", "text": "TEXT", "source": "docs", "file_path": "y.html"},
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert n_dropped == 0
        assert len(deduped) == 2

    def test_empty_input(self):
        deduped, n_dropped = ingest_docs.dedup_records([])
        assert deduped == []
        assert n_dropped == 0

    def test_single_record(self):
        recs = [_rec("docs", "a.html")]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 1
        assert n_dropped == 0

    def test_n_dropped_accurate(self):
        recs = [_rec("docs", f"{i}.html") for i in range(10)]  # all same content
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 1
        assert n_dropped == 9

    def test_multiple_groups(self):
        """Multiple distinct hashes each deduplicated independently."""
        recs = [
            _rec("docs", "a.html", text="GROUP_A"),
            _rec("docs", "b.html", text="GROUP_A"),
            _rec("docs", "c.html", text="GROUP_B"),
            _rec("docs", "d.html", text="GROUP_B"),
            _rec("docs", "e.html", text="GROUP_C"),
        ]
        deduped, n_dropped = ingest_docs.dedup_records(recs)
        assert len(deduped) == 3  # one per distinct hash
        assert n_dropped == 2

    def test_source_priority_table_covers_all_sources(self):
        """Every key in SOURCE_META should have an entry or default to 0 gracefully."""
        for source in ingest_docs.SOURCE_META:
            priority = ingest_docs._DEDUP_SOURCE_PRIORITY.get(source, 0)
            assert isinstance(priority, int)
