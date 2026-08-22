#!/usr/bin/env python3
"""Materialize retrieval metadata in an existing LanceDB docs index.

This migration preserves stable chunk IDs and existing embeddings, so it is
safe for a prebuilt index whose source tree is no longer complete. A full
ingestion rebuild remains the preferred path when all sources are available.

Usage:
    uv run python scripts/migrate_rag_metadata.py
    uv run python scripts/migrate_rag_metadata.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hpe_networking_mcp.pipeline.clients import lance_client  # noqa: E402
from ingestion.ingest_docs import RAG_METADATA_FIELDS, derive_metadata  # noqa: E402

STAGING_TABLE = f"{lance_client.DOCS_TABLE}__metadata_staging"
DEFAULT_BATCH_SIZE = 2048


def _rows_in_batches(table: Any, batch_size: int) -> Iterable[list[dict[str, Any]]]:
    """Read all columns in bounded windows without loading the corpus at once."""
    columns = list(table.schema.names)
    offset = 0
    while True:
        batch = table.search().select(columns).limit(batch_size).offset(offset).to_arrow()
        if batch.num_rows == 0:
            return
        yield batch.to_pylist()
        offset += batch.num_rows


def _with_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Return one legacy row with deterministic metadata fields populated."""
    metadata = derive_metadata(
        str(row.get("source") or ""),
        str(row.get("file_path") or ""),
        row.get("source_url"),
        record_type=str(row.get("record_type") or "document"),
    )
    for field in RAG_METADATA_FIELDS:
        existing = row.get(field)
        if existing not in (None, ""):
            metadata[field] = existing
    return {**row, **metadata}


def _arrow_batch(rows: list[dict[str, Any]], source_schema: pa.Schema) -> pa.Table:
    """Build batches with stable types for nullable legacy and new fields."""
    batch = pa.Table.from_pylist(rows)
    string_fields = set(RAG_METADATA_FIELDS)
    for field in source_schema:
        if field.name not in string_fields:
            continue
        values = [
            None if row.get(field.name) in (None, "") else str(row[field.name])
            for row in rows
        ]
        batch = batch.set_column(
            batch.schema.get_field_index(field.name),
            field.name,
            pa.array(values, type=pa.string()),
        )
    for field in source_schema:
        if field.name in string_fields or field.name not in batch.schema.names:
            continue
        index = batch.schema.get_field_index(field.name)
        if pa.types.is_null(batch.schema.field(index).type):
            values = [row.get(field.name) for row in rows]
            batch = batch.set_column(
                index,
                field.name,
                pa.array(values, type=field.type),
            )
    for field in RAG_METADATA_FIELDS:
        if field in source_schema.names:
            continue
        values = [
            None if row.get(field) in (None, "") else str(row[field])
            for row in rows
        ]
        batch = batch.set_column(
            batch.schema.get_field_index(field),
            field,
            pa.array(values, type=pa.string()),
        )
    return batch


def migrate_metadata(
    *,
    data_dir: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict[str, int | bool | str]:
    """Migrate the docs table and rebuild retrieval indexes."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    db = lance_client.connect(data_dir)
    table = lance_client.docs_table(db)
    if table is None:
        raise FileNotFoundError(
            f"LanceDB docs table missing under {data_dir}; build or download "
            "the index before running this migration"
        )

    missing = sorted(set(RAG_METADATA_FIELDS) - set(table.schema.names))
    if not missing:
        return {
            "ok": True,
            "migrated": False,
            "rows": table.count_rows(),
            "missing_columns": 0,
        }

    if dry_run:
        return {
            "ok": True,
            "migrated": False,
            "rows": table.count_rows(),
            "missing_columns": len(missing),
        }

    table_listing = db.list_tables()
    table_names = getattr(table_listing, "tables", table_listing)
    if STAGING_TABLE in table_names:
        db.drop_table(STAGING_TABLE)

    staging = None
    rows_seen = 0
    source_schema = table.schema
    for rows in _rows_in_batches(table, batch_size):
        enriched = [_with_metadata(row) for row in rows]
        arrow_batch = _arrow_batch(enriched, source_schema)
        if staging is None:
            staging = db.create_table(STAGING_TABLE, data=arrow_batch)
        else:
            staging.add(arrow_batch)
        rows_seen += len(enriched)
        print(f"  migrated {rows_seen}/{table.count_rows()}", flush=True)

    if staging is None:
        raise ValueError("docs table contains no rows; refusing empty migration")

    live = lance_client.promote_staging_table(db, STAGING_TABLE)
    lance_client.build_search_indexes(live)
    final_columns = set(live.schema.names)
    missing_after = sorted(set(RAG_METADATA_FIELDS) - final_columns)
    if missing_after:
        raise RuntimeError(f"metadata migration incomplete: {missing_after}")
    return {
        "ok": True,
        "migrated": True,
        "rows": rows_seen,
        "missing_columns": len(missing),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="LanceDB directory (default: repository data/)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows copied per batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report missing metadata columns without changing the index",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = migrate_metadata(
        data_dir=args.data_dir,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
