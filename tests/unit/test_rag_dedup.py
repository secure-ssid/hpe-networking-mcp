"""Tests for _dedup_by_content: content-hash deduplication of search hits."""

from hpe_networking_mcp.mcp_servers.rag import _dedup_by_content, _shape


def _hit(text, file_path, score, content_hash=None, source="docs", doc_type="guide"):
    h = {
        "text": text,
        "file_path": file_path,
        "score": score,
        "source": source,
        "doc_type": doc_type,
    }
    if content_hash is not None:
        h["content_hash"] = content_hash
    return h


class TestDedupByContent:
    def test_no_duplicates_unchanged(self):
        hits = [
            _hit("AAA", "a.html", 0.9, "hash1"),
            _hit("BBB", "b.html", 0.7, "hash2"),
        ]
        result = _dedup_by_content(hits)
        assert len(result) == 2
        assert {r["file_path"] for r in result} == {"a.html", "b.html"}

    def test_exact_duplicates_collapsed_to_one(self):
        hits = [
            _hit("SAME", "a/file.html", 0.9, "dup"),
            _hit("SAME", "b/file.html", 0.5, "dup"),
            _hit("SAME", "c/file.html", 0.3, "dup"),
        ]
        result = _dedup_by_content(hits)
        assert len(result) == 1
        assert result[0]["file_path"] == "a/file.html"  # highest score wins
        assert result[0]["score"] == 0.9
        assert "also_in" in result[0]
        assert len(result[0]["also_in"]) == 2

    def test_best_score_representative_kept(self):
        """The hit with the highest score becomes the representative."""
        hits = [
            _hit("SAME", "low.html", 0.2, "h1"),
            _hit("SAME", "mid.html", 0.6, "h1"),
            _hit("SAME", "high.html", 0.9, "h1"),
        ]
        result = _dedup_by_content(hits)
        assert len(result) == 1
        assert result[0]["file_path"] == "high.html"
        assert result[0]["score"] == 0.9

    def test_hits_without_hash_pass_through(self):
        """Rows with no content_hash (legacy) are never collapsed."""
        hits = [
            _hit("T1", "a.html", 0.8),  # no content_hash
            _hit("T2", "b.html", 0.7),  # no content_hash
        ]
        result = _dedup_by_content(hits)
        assert len(result) == 2

    def test_mixed_hashed_and_unhashed(self):
        hits = [
            _hit("DUP", "a.html", 0.9, "dup"),
            _hit("DUP", "b.html", 0.5, "dup"),
            _hit("UNIQUE", "c.html", 0.4),  # no hash
        ]
        result = _dedup_by_content(hits)
        assert len(result) == 2
        paths = {r["file_path"] for r in result}
        assert "a.html" in paths
        assert "c.html" in paths

    def test_ordering_preserved_by_score(self):
        hits = [
            _hit("AAA", "a.html", 0.9, "h1"),
            _hit("BBB", "b.html", 0.7, "h2"),
            _hit("AAA", "c.html", 0.3, "h1"),
        ]
        result = _dedup_by_content(hits)
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_also_in_capped_at_five(self):
        """also_in list is bounded to 5 paths to keep response size bounded."""
        hits = [_hit("TEXT", f"{i}.html", 1.0 - i * 0.1, "dupe") for i in range(8)]
        result = _dedup_by_content(hits)
        assert len(result) == 1
        assert len(result[0].get("also_in", [])) <= 5

    def test_empty_input(self):
        assert _dedup_by_content([]) == []

    def test_single_hit(self):
        hits = [_hit("Only", "x.html", 0.5, "h")]
        result = _dedup_by_content(hits)
        assert len(result) == 1
        assert "also_in" not in result[0]


class TestShapeAlsoIn:
    def test_also_in_propagated_to_output(self):
        rows = [
            {
                "text": "Some content",
                "source": "docs",
                "doc_type": "guide",
                "file_path": "a.html",
                "score": 0.9,
                "also_in": ["b.html", "c.html"],
            }
        ]
        shaped = _shape(rows, top_k=5)
        assert shaped[0]["also_in"] == ["b.html", "c.html"]

    def test_also_in_absent_when_no_duplicates(self):
        rows = [
            {
                "text": "X",
                "source": "docs",
                "doc_type": "guide",
                "file_path": "a.html",
                "score": 0.5,
            }
        ]
        shaped = _shape(rows, top_k=5)
        assert "also_in" not in shaped[0]
