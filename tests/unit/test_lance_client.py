"""Unit tests for the LanceDB embedded store.

Uses small fixture tables with deterministic pseudo-random vectors — no
embedding model, no network, no servers.

Test bar:
- hybrid search returns redis_client.vector_search-shaped rows
- BM25 half of hybrid surfaces exact-keyword matches even with junk vectors
- source filter narrows results; malformed filter raises (no SQL injection)
- missing docs table raises FileNotFoundError with build instructions
- source_counts reports per-source chunk counts (the R2 post-ingest assert)
- tools table: hybrid search returns search_tools-shaped rows
"""

from __future__ import annotations

import random

import pytest

from hpe_networking_mcp.pipeline.clients import lance_client as lc


def _vec(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(768)]


@pytest.fixture
def db(tmp_path):
    db = lc.connect(tmp_path)
    rows = [
        {"id": "1", "text": "Create a WPA3 SSID with SAE security on the wireless network",
         "source": "developer_docs", "doc_type": "developer-docs",
         "file_path": "ssid.md", "chunk_index": 0, "vector": _vec(1)},
        {"id": "2", "text": "Configure the L2 VLAN on the access switch port profile",
         "source": "tech_docs", "doc_type": "tech-docs",
         "file_path": "vlan.md", "chunk_index": 1, "vector": _vec(2)},
        {"id": "3", "text": "Passpoint identity profiles use 802.11u for public access",
         "source": "vsg_docs", "doc_type": "vsg",
         "file_path": "passpoint.md", "chunk_index": 2, "vector": _vec(3)},
    ]
    table = lc.create_docs_table(db, rows)
    lc.build_fts_index(table)
    return db


class TestHybridSearch:
    def test_result_shape_matches_redis_contract(self, db):
        hits = lc.hybrid_search(db, "WPA3 SSID", _vec(99), top_k=2)
        assert hits
        for h in hits:
            assert set(h) == {"text", "source", "doc_type", "file_path",
                              "chunk_index", "score"}
            assert isinstance(h["chunk_index"], int)

    def test_result_includes_optional_provenance(self, tmp_path):
        db = lc.connect(tmp_path)
        rows = [{
            "id": "1",
            "text": "WPA3 guide",
            "source": "developer_docs",
            "doc_type": "developer-docs",
            "file_path": "ssid.md",
            "chunk_index": 0,
            "source_url": "https://example.com/ssid",
            "heading_breadcrumb": "Wireless > Security",
            "vector": _vec(1),
        }]
        table = lc.create_docs_table(db, rows)
        lc.build_fts_index(table)

        hits = lc.hybrid_search(db, "WPA3 guide", _vec(99), top_k=1)

        assert hits[0]["source_url"] == "https://example.com/ssid"
        assert hits[0]["heading_breadcrumb"] == "Wireless > Security"

    def test_bm25_surfaces_exact_keyword_despite_junk_vector(self, db):
        # the query vector is random noise — only the FTS half can rank this
        hits = lc.hybrid_search(db, "WPA3 SAE SSID", _vec(99), top_k=1)
        assert hits[0]["file_path"] == "ssid.md"

    def test_negative_top_k_clamped_to_one(self, db):
        hits = lc.hybrid_search(db, "WPA3 SAE SSID", _vec(99), top_k=-5)
        assert len(hits) == 1

    def test_source_filter_narrows(self, db):
        hits = lc.hybrid_search(db, "VLAN port profile", _vec(99), top_k=3,
                                source_filter="tech_docs")
        assert hits and all(h["source"] == "tech_docs" for h in hits)

    def test_multi_source_filter_narrows_to_any_allowed_source(self, db):
        hits = lc.hybrid_search(
            db,
            "SSID VLAN Passpoint",
            _vec(99),
            top_k=3,
            source_filter=("developer_docs", "vsg_docs"),
        )
        assert hits
        assert {h["source"] for h in hits} <= {"developer_docs", "vsg_docs"}
        assert {h["source"] for h in hits} == {"developer_docs", "vsg_docs"}

    def test_metadata_filter_narrows_results(self, tmp_path):
        db = lc.connect(tmp_path)
        rows = [
            {
                "id": "1",
                "text": "Aruba WPA3 guide",
                "source": "developer_docs",
                "doc_type": "guide",
                "file_path": "aruba.md",
                "chunk_index": 0,
                "vendor": "aruba",
                "product": "central",
                "vector": _vec(1),
            },
            {
                "id": "2",
                "text": "Juniper WPA3 guide",
                "source": "mist_docs",
                "doc_type": "guide",
                "file_path": "juniper.md",
                "chunk_index": 0,
                "vendor": "juniper",
                "product": "mist",
                "vector": _vec(2),
            },
        ]
        table = lc.create_docs_table(db, rows)
        lc.build_fts_index(table)

        hits = lc.hybrid_search(
            db,
            "WPA3 guide",
            _vec(99),
            top_k=3,
            metadata_filter={"vendor": "aruba"},
        )

        assert hits and all(hit["vendor"] == "aruba" for hit in hits)

    def test_metadata_filter_rejects_unknown_fields(self, db):
        with pytest.raises(ValueError, match="invalid metadata filter field"):
            lc.hybrid_search(db, "x", _vec(99), metadata_filter={"where": "bad"})

    def test_malformed_source_filter_raises(self, db):
        with pytest.raises(ValueError, match="invalid source filter"):
            lc.hybrid_search(db, "x", _vec(99), source_filter="bad'; DROP--")

    def test_malformed_multi_source_filter_raises(self, db):
        with pytest.raises(ValueError, match="invalid source filter"):
            lc.hybrid_search(db, "x", _vec(99), source_filter=("tech_docs", "bad'}"))

    def test_missing_table_raises_with_build_instructions(self, tmp_path):
        empty = lc.connect(tmp_path / "empty")
        with pytest.raises(FileNotFoundError, match="ingest_docs"):
            lc.hybrid_search(empty, "anything", _vec(99))


class TestCounts:
    def test_doc_count_and_source_counts(self, db):
        assert lc.doc_count(db) == 3
        assert lc.source_counts(db) == {
            "developer_docs": 1, "tech_docs": 1, "vsg_docs": 1,
        }

    def test_empty_db_counts(self, tmp_path):
        empty = lc.connect(tmp_path / "empty")
        assert lc.doc_count(empty) == 0
        assert lc.source_counts(empty) == {}


class TestSearchIndexes:
    def test_build_search_indexes_creates_fts_and_vector_indexes(self, db):
        table = lc.docs_table(db)
        lc.build_search_indexes(table)

        indexes = {index.name: index for index in table.list_indices()}
        assert indexes[lc.FTS_INDEX_NAME].index_type == "FTS"
        assert indexes[lc.VECTOR_INDEX_NAME].columns == ["vector"]
        assert indexes["source_idx"].index_type == "BTree"

    def test_small_tables_keep_fts_without_untrainable_ann_index(self, tmp_path):
        db = lc.connect(tmp_path)
        rows = [{
            "id": "1",
            "text": "small",
            "source": "docs",
            "doc_type": "docs",
            "file_path": "small.md",
            "chunk_index": 0,
            "vector": _vec(1),
        }]
        table = lc.create_docs_table(db, rows)

        assert lc.build_search_indexes(table) is False
        names = {index.name for index in table.list_indices()}
        assert lc.FTS_INDEX_NAME in names
        assert "source_idx" in names
        assert lc.VECTOR_INDEX_NAME not in names


class TestToolsTable:
    def test_hybrid_tool_search_shape_and_keyword_match(self, db):
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan create_vlan Create a VLAN", "vector": _vec(4)},
            {"id": "t2", "server": "central-ops", "name": "reboot_device",
             "description": "Reboot a device", "schema_json": "{}",
             "fts_text": "reboot device reboot_device Reboot a device", "vector": _vec(5)},
        ]
        lc.create_tools_table(db, rows)
        hits = lc.search_tools(db, "create a vlan", _vec(99), top_k=1)
        assert hits[0]["name"] == "create_vlan"
        assert set(hits[0]) == {"name", "description", "server", "schema_json", "score"}

    def test_negative_tool_search_top_k_clamped_to_one(self, db):
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan create_vlan Create a VLAN", "vector": _vec(4)},
            {"id": "t2", "server": "central-ops", "name": "reboot_device",
             "description": "Reboot a device", "schema_json": "{}",
             "fts_text": "reboot device reboot_device Reboot a device", "vector": _vec(5)},
        ]
        lc.create_tools_table(db, rows)
        hits = lc.search_tools(db, "create a vlan", _vec(99), top_k=-5)
        assert len(hits) == 1

    def test_missing_tools_table_returns_empty(self, tmp_path):
        empty = lc.connect(tmp_path / "empty")
        assert lc.search_tools(empty, "anything", _vec(99)) == []

    def test_servers_filter_excludes_disabled_backends(self, db):
        """A deployment only enables some backends; the rest must not surface.

        The tools table holds the complete catalog, so without a prefilter the
        deep fetch could return nothing but tools from disabled backends.
        """
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan create_vlan Create a VLAN", "vector": _vec(4)},
            {"id": "t2", "server": "clearpass-core", "name": "cppm_create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan cppm_create_vlan Create a VLAN", "vector": _vec(4)},
        ]
        lc.create_tools_table(db, rows)

        hits = lc.search_tools(
            db, "create a vlan", _vec(4), top_k=10, servers=["central-config"]
        )
        assert [h["name"] for h in hits] == ["create_vlan"]

    def test_servers_filter_keeps_enabled_backend_hits(self, db):
        """The filter must narrow to the enabled set, not drop everything."""
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan create_vlan Create a VLAN", "vector": _vec(4)},
            {"id": "t2", "server": "glp-core", "name": "glp_preflight",
             "description": "Inspect local GLP readiness", "schema_json": "{}",
             "fts_text": "glp preflight local readiness", "vector": _vec(6)},
        ]
        lc.create_tools_table(db, rows)

        hits = lc.search_tools(
            db, "create a vlan", _vec(4), top_k=10,
            servers=["central-config", "glp-core"],
        )
        assert "create_vlan" in {h["name"] for h in hits}

    def test_no_servers_filter_searches_whole_catalog(self, db):
        """Omitting ``servers`` must preserve the previous unfiltered behaviour."""
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan create_vlan Create a VLAN", "vector": _vec(4)},
            {"id": "t2", "server": "clearpass-core", "name": "cppm_create_vlan",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan cppm_create_vlan Create a VLAN", "vector": _vec(4)},
        ]
        lc.create_tools_table(db, rows)

        hits = lc.search_tools(db, "create a vlan", _vec(4), top_k=10)
        assert {h["server"] for h in hits} == {"central-config", "clearpass-core"}

    def test_server_name_with_quote_does_not_break_filter(self, db):
        """Backend names are internal, but the SQL predicate is string-built."""
        rows = [
            {"id": "t1", "server": "it's-core", "name": "odd_backend_tool",
             "description": "Create a VLAN", "schema_json": "{}",
             "fts_text": "create vlan odd_backend_tool Create a VLAN", "vector": _vec(4)},
        ]
        lc.create_tools_table(db, rows)

        hits = lc.search_tools(
            db, "create a vlan", _vec(4), top_k=10, servers=["it's-core"]
        )
        assert [h["name"] for h in hits] == ["odd_backend_tool"]

    def test_merge_tool_rows_upserts_without_rebuilding_table(self, db):
        rows = [
            {"id": "t1", "server": "central-config", "name": "create_vlan",
             "description": "Updated VLAN creation", "schema_json": "{}",
             "fts_text": "create vlan updated", "vector": _vec(4)},
            {"id": "t3", "server": "glp-core", "name": "glp_preflight",
             "description": "Inspect local GLP readiness", "schema_json": "{}",
             "fts_text": "glp preflight local readiness", "vector": _vec(6)},
        ]
        lc.merge_tools_rows(db, rows)

        assert lc.tool_count(db) == 2
        hits = lc.search_tools(db, "local GLP readiness", _vec(99), top_k=1)
        assert hits[0]["name"] == "glp_preflight"


class TestPromoteStagingTable:
    def test_swap_replaces_live_and_drops_staging(self, tmp_path):
        db = lc.connect(tmp_path)
        # A stale "live" table that must be fully replaced by the staged data.
        lc.create_docs_table(
            db,
            [{"id": "old", "text": "stale", "source": "developer_docs",
              "doc_type": "developer-docs", "file_path": "old.md",
              "chunk_index": 0, "content_hash": "old-hash", "vector": _vec(1)}],
        )
        staging = f"{lc.DOCS_TABLE}__staging"
        lc.create_docs_table(
            db,
            [{"id": "new", "text": "fresh", "source": "tech_docs",
              "doc_type": "tech-docs", "file_path": "new.md",
              "chunk_index": 0, "content_hash": "new-hash", "vector": _vec(2)}],
            table_name=staging,
        )

        lc.promote_staging_table(db, staging)

        ids = {row["id"] for row in lc.docs_metadata(db)}
        assert ids == {"new"}  # old fully replaced
        # Staging table dropped after promotion.
        assert lc.docs_table(db, staging) is None


class TestFtsFallback:
    def test_hybrid_search_falls_back_to_vector_only_without_fts(self, tmp_path):
        db = lc.connect(tmp_path)
        # Build the docs table but deliberately DO NOT build the FTS index —
        # mimics a crash between the staging swap and build_fts_index.
        lc.create_docs_table(
            db,
            [{"id": "1", "text": "Create a WPA3 SSID with SAE security",
              "source": "developer_docs", "doc_type": "developer-docs",
              "file_path": "ssid.md", "chunk_index": 0, "vector": _vec(1)},
             {"id": "2", "text": "Configure the VLAN on the switch",
              "source": "tech_docs", "doc_type": "tech-docs",
              "file_path": "vlan.md", "chunk_index": 1, "vector": _vec(2)}],
        )

        hits = lc.hybrid_search(db, "WPA3 SSID", _vec(1), top_k=2)

        assert hits  # degraded, not errored
        for h in hits:
            assert set(h) == {"text", "source", "doc_type", "file_path",
                              "chunk_index", "score"}
            assert isinstance(h["score"], float)
