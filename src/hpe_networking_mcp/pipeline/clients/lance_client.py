"""LanceDB embedded document store — hybrid (vector + BM25) search, no server.

Replaces the Redis Stack backend for the default download-and-run path: the
whole index lives in data/ (docs.lance + tools.lance), ships prebuilt via
GitHub Release, and rebuilds with `uv run python ingestion/ingest_docs.py`.

Hybrid search (R5): vector similarity + native BM25 FTS, fused with Reciprocal
Rank Fusion — this catches exact identifiers (WPA3_SAE, endpoint paths) that
pure vector search misses, replacing the static _SOURCE_BOOST re-rank.
Result rows match redis_client.vector_search's shape so rag.py can treat the
backends interchangeably: {text, source, doc_type, file_path, chunk_index, score}.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

logger = logging.getLogger(__name__)

# Repository root, not the installed package directory: under the ``src/``
# layout ``parents[2]`` resolves to ``src/hpe_networking_mcp``, which made
# LanceDB create an empty ``src/hpe_networking_mcp/data`` instead of reading
# the repo-root ``data/docs.lance`` + ``data/tools.lance`` indexes.
ROOT = repo_root()
DATA_DIR = ROOT / "data"
DOCS_TABLE = "docs"
TOOLS_TABLE = "tools"
EMBEDDING_DIMS = 768
MAX_SEARCH_TOP_K = 200

_SOURCE_RE = re.compile(r"^[a-z0-9_]+$")
_DOC_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
SourceFilter = str | tuple[str, ...] | list[str] | None


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(top_k, MAX_SEARCH_TOP_K))


def connect(data_dir: Path = DATA_DIR):
    """Open (or create) the embedded LanceDB database directory."""
    import lancedb
    data_dir.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(data_dir)


def docs_table(db, table_name: str = DOCS_TABLE):
    """Return the docs table, or None if it hasn't been built yet."""
    try:
        return db.open_table(table_name)
    except Exception:
        return None


def create_docs_table(db, rows: list[dict[str, Any]], table_name: str = DOCS_TABLE):
    """Create the docs table fresh from rows that already carry a 'vector' key."""
    return db.create_table(table_name, data=rows, mode="overwrite")


_PROMOTE_WINDOW = 4096


def promote_staging_table(db, staging_table_name: str, table_name: str = DOCS_TABLE):
    """Atomically replace ``table_name`` with the contents of a fully-built
    staging table, then drop the staging table.

    LanceDB OSS has no ``rename_table``, so the swap is one
    ``create_table(..., mode="overwrite")`` call — Lance's versioned storage
    format commits it atomically, so a crash mid-swap leaves the previous
    ``table_name`` version intact rather than a partially-written table. The
    staging data is streamed in windows through a RecordBatchReader rather
    than materialized with ``to_arrow()``, so peak memory stays at one window
    (~4k rows) instead of the whole corpus. Callers should only call this
    after the staging table has been fully populated and validated (e.g. the
    R2 per-source empty check) — this function performs no validation itself.
    """
    import pyarrow as pa

    staging = db.open_table(staging_table_name)

    def _windows():
        offset = 0
        while True:
            chunk = staging.search().limit(_PROMOTE_WINDOW).offset(offset).to_arrow()
            if chunk.num_rows == 0:
                return
            yield from chunk.to_batches()
            offset += _PROMOTE_WINDOW

    reader = pa.RecordBatchReader.from_batches(staging.schema, _windows())
    live = db.create_table(table_name, data=reader, mode="overwrite")
    db.drop_table(staging_table_name)
    return live


def docs_columns(db, table_name: str = DOCS_TABLE) -> set[str]:
    table = docs_table(db, table_name)
    return set(table.schema.names) if table is not None else set()


def docs_metadata(
    db,
    table_name: str = DOCS_TABLE,
) -> list[dict[str, Any]]:
    """Return lightweight row metadata used by incremental ingestion."""
    table = docs_table(db, table_name)
    if table is None:
        return []
    arrow = (
        table.search()
        .select(["id", "content_hash", "source", "file_path"])
        .limit(table.count_rows())
        .to_arrow()
    )
    return arrow.to_pylist()


def merge_docs_rows(
    db,
    rows: list[dict[str, Any]],
    table_name: str = DOCS_TABLE,
) -> None:
    """Upsert already-embedded document rows by stable chunk ID."""
    if not rows:
        return
    table = docs_table(db, table_name)
    if table is None:
        create_docs_table(db, rows, table_name)
        return
    (
        table.merge_insert("id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(rows)
    )


def delete_docs_ids(
    db,
    ids: list[str],
    table_name: str = DOCS_TABLE,
) -> None:
    """Delete removed chunk IDs in bounded predicates."""
    table = docs_table(db, table_name)
    if table is None:
        return
    for start in range(0, len(ids), 200):
        batch = ids[start : start + 200]
        if not all(_DOC_ID_RE.fullmatch(value) for value in batch):
            raise ValueError("invalid document id in incremental delete")
        values = ", ".join(f"'{value}'" for value in batch)
        table.delete(f"id IN ({values})")


def build_fts_index(table) -> None:
    """Build the native BM25 FTS index over text (call once after all adds)."""
    from lancedb.index import FTS

    table.create_index("text", config=FTS(), replace=True)


def _source_where_clause(source_filter: SourceFilter) -> str | None:
    if not source_filter:
        return None
    values = (source_filter,) if isinstance(source_filter, str) else tuple(source_filter)
    if not values or any(
        not isinstance(value, str) or not _SOURCE_RE.match(value) for value in values
    ):
        raise ValueError(f"invalid source filter: {source_filter!r}")
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"source IN ({quoted})" if len(values) > 1 else f"source = '{values[0]}'"


def hybrid_search(
    db,
    query_text: str,
    query_vector: list[float],
    top_k: int = 15,
    source_filter: SourceFilter = None,
    table_name: str = DOCS_TABLE,
) -> list[dict[str, Any]]:
    """Hybrid (vector + BM25, RRF-fused) search over the docs table.

    Returns rows shaped like redis_client.vector_search; score is the fused
    relevance score (higher = better).
    """
    table = docs_table(db, table_name)
    if table is None:
        raise FileNotFoundError(
            f"LanceDB docs table missing under {DATA_DIR} — build it with "
            "`uv run python ingestion/ingest_docs.py` or download the prebuilt "
            "index from the GitHub Release."
        )
    top_k = _clamp_top_k(top_k)
    source_where = _source_where_clause(source_filter)
    # limit() truncates EACH leg (vector, FTS) before RRF fusion — fetch deep
    # so fusion sees real overlap, then slice to top_k after.
    q = (
        table.search(query_type="hybrid")
        .vector(query_vector)
        .text(query_text)
        .limit(max(top_k * 3, 15))
    )
    if source_where:
        q = q.where(source_where, prefilter=True)
    try:
        rows = q.to_list()
        score_key = "_relevance_score"
    except Exception as exc:
        if "full text search" not in str(exc).lower() and "inverted" not in str(exc).lower():
            raise
        # The FTS index is missing — e.g. a rebuild crashed between the
        # staging-table swap and build_fts_index, or a query landed during
        # the post-swap index build. Degrade to vector-only search so
        # ask_docs/search_docs keep working instead of erroring on every
        # call until the next successful rebuild.
        logger.warning(
            "FTS index missing on %s — falling back to vector-only search. "
            "Rebuild with `uv run python ingestion/ingest_docs.py`.",
            table_name,
        )
        vq = table.search(query_vector).limit(max(top_k * 3, 15))
        if source_where:
            vq = vq.where(source_where, prefilter=True)
        rows = vq.to_list()
        score_key = None
    hits = []
    for r in rows[:top_k]:
        if score_key is not None:
            score = float(r.get(score_key, 0.0))
        else:
            # Vector-only: convert L2 distance to a higher-is-better score.
            score = 1.0 / (1.0 + float(r.get("_distance", 0.0)))
        hits.append({
            "text": r.get("text", ""),
            "source": r.get("source", ""),
            "doc_type": r.get("doc_type", ""),
            "file_path": r.get("file_path", ""),
            "chunk_index": int(r.get("chunk_index", 0) or 0),
            "score": round(score, 4),
        })
    return hits


def doc_count(db, table_name: str = DOCS_TABLE) -> int:
    table = docs_table(db, table_name)
    return table.count_rows() if table is not None else 0


def source_counts(db, table_name: str = DOCS_TABLE) -> dict[str, int]:
    """Chunks per source — the post-ingest 'every source has >0 docs' assert (R2)."""
    table = docs_table(db, table_name)
    if table is None:
        return {}
    tbl = (
        table.search()
        .select(["source"])
        .limit(table.count_rows())
        .to_arrow()
    )
    counts: dict[str, int] = {}
    for s in tbl.column("source").to_pylist():
        counts[s] = counts.get(s, 0) + 1
    return counts


# ── Tools index (backs tool_router.find_tool) ────────────────────────────────


def create_tools_table(db, rows: list[dict[str, Any]], table_name: str = TOOLS_TABLE):
    """Create the tools table fresh. Rows: server/name/description/schema_json/vector.

    Each row must also carry a 'fts_text' column (name + description combined)
    — the FTS half of hybrid tool search runs over it.
    """
    from lancedb.index import FTS

    table = db.create_table(table_name, data=rows, mode="overwrite")
    table.create_index("fts_text", config=FTS(), replace=True)
    return table


def tools_table(db, table_name: str = TOOLS_TABLE):
    """Return the tools table, or None if it hasn't been built yet."""
    try:
        return db.open_table(table_name)
    except Exception:
        return None


def tool_count(db, table_name: str = TOOLS_TABLE) -> int | None:
    """Return indexed tool count, or None when the tools table is missing."""
    table = tools_table(db, table_name)
    return table.count_rows() if table is not None else None


def search_tools(
    db,
    query_text: str,
    query_vector: list[float],
    top_k: int = 10,
    table_name: str = TOOLS_TABLE,
) -> list[dict[str, Any]]:
    """Hybrid search over tool definitions (same shape as redis search_tools)."""
    table = tools_table(db, table_name)
    if table is None:
        return []
    top_k = _clamp_top_k(top_k)
    q = (
        table.search(query_type="hybrid")
        .vector(query_vector)
        .text(query_text)
        .limit(max(top_k * 3, 15))  # deep fetch per leg, fuse, then slice
    )
    hits = []
    for r in q.to_list()[:top_k]:
        hits.append({
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "server": r.get("server", ""),
            "schema_json": r.get("schema_json", "{}"),
            "score": round(float(r.get("_relevance_score", 0.0)), 4),
        })
    return hits
