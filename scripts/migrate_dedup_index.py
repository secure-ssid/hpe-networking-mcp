#!/usr/bin/env python3
"""Remove exact-duplicate rows from an existing LanceDB docs index.

This script deduplicates the current docs index in-place using a staging
table, without re-downloading sources or re-embedding any content. It is the
recommended path when you have a prebuilt index and want to recover the ~38%
space overhead from repeated boilerplate chunks.

For new full rebuilds, prefer ``ingest_docs.py --dedup-on-ingest`` instead.

What "duplicate" means:
    Two rows are duplicates when they share the same ``content_hash``. Only one
    representative is kept — the one from the highest-authority source per
    ``_DEDUP_SOURCE_PRIORITY``. Ties are broken by lexicographic file_path
    order so the selection is stable across runs.

Safety:
    Rows are streamed into a staging table first. The live table is replaced
    only after the staging table has been fully populated and validated.
    A crash before the swap leaves the existing good index untouched.

Usage:
    uv run python scripts/migrate_dedup_index.py
    uv run python scripts/migrate_dedup_index.py --dry-run    # count only
    uv run python scripts/migrate_dedup_index.py --batch-size 1024
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC), str(ROOT / "ingestion")):
    if p not in sys.path:
        sys.path.insert(0, p)

from hpe_networking_mcp.pipeline.clients import lance_client  # noqa: E402
from ingestion.ingest_docs import _DEDUP_SOURCE_PRIORITY  # noqa: E402

STAGING_TABLE = f"{lance_client.DOCS_TABLE}__dedup_staging"
DEFAULT_BATCH_SIZE = 2048


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_in_batches(table: Any, batch_size: int) -> Iterable[list[dict[str, Any]]]:
    """Read all rows in bounded windows without loading the corpus at once."""
    columns = list(table.schema.names)
    offset = 0
    while True:
        batch = (
            table.search()
            .select(columns)
            .limit(batch_size)
            .offset(offset)
            .to_arrow()
        )
        if batch.num_rows == 0:
            return
        yield batch.to_pylist()
        offset += batch.num_rows


def _source_priority(row: dict[str, Any]) -> int:
    return _DEDUP_SOURCE_PRIORITY.get(row.get("source", ""), 0)


def _build_canonical_map(table: Any, batch_size: int) -> dict[str, dict[str, Any]]:
    """Return {content_hash: best_row} for all rows in the table.

    "Best" = highest source priority; ties broken by file_path lexicographic
    order.  Rows without a content_hash are stored under a synthetic unique
    key (id) so they are never accidentally collapsed.
    """
    best: dict[str, dict[str, Any]] = {}
    total = 0
    for batch in _rows_in_batches(table, batch_size):
        for row in batch:
            total += 1
            ch = row.get("content_hash") or None
            key = ch if ch else f"__nohash_{row['id']}"
            existing = best.get(key)
            if existing is None:
                best[key] = row
            else:
                ep = _source_priority(existing)
                rp = _source_priority(row)
                if rp > ep or (
                    rp == ep and row.get("file_path", "") < existing.get("file_path", "")
                ):
                    best[key] = row
        print(f"  scanned {total} rows …", end="\r", flush=True)
    print(f"  scanned {total} rows total")
    return best


def _to_arrow_batch(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    """Convert a list of dicts to a typed Arrow table matching the given schema."""
    keys = [f.name for f in schema]
    typed: dict[str, list] = {k: [] for k in keys}
    for row in rows:
        for k in keys:
            typed[k].append(row.get(k))
    arrays = []
    for field in schema:
        raw = typed[field.name]
        if pa.types.is_large_list(field.type) or pa.types.is_list(field.type):
            arrays.append(pa.array(raw, type=field.type))
        elif pa.types.is_fixed_size_list(field.type):
            arrays.append(pa.array(raw, type=field.type))
        else:
            arrays.append(
                pa.array(raw, type=pa.string() if pa.types.is_string(field.type) else field.type)
            )
    return pa.table(arrays, schema=schema)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
    db = lance_client.connect()
    table = lance_client.docs_table(db)
    if table is None:
        raise SystemExit("docs table not found — run ingest_docs.py first")

    total_rows = table.count_rows()
    print(f"docs table: {total_rows:,} rows")

    print("\nBuilding canonical map (scanning all rows)…")
    canonical = _build_canonical_map(table, batch_size)
    n_unique = len(canonical)
    n_dropped = total_rows - n_unique

    by_source = Counter(row.get("source", "?") for row in canonical.values())
    print(
        f"\nAfter dedup: {n_unique:,} unique rows, "
        f"{n_dropped:,} dropped ({100*n_dropped//total_rows}%)"
    )
    print("Canonical rows per source:")
    for src, cnt in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {src}: {cnt:,}")

    if dry_run:
        print("\nDry run — no changes written.")
        return

    if n_dropped == 0:
        print("\nIndex already deduplicated — nothing to do.")
        return

    schema = table.schema
    rows_to_write = list(canonical.values())

    # Drop any residual staging table from a previous interrupted run
    try:
        db.drop_table(STAGING_TABLE)
        print(f"  dropped stale staging table '{STAGING_TABLE}'")
    except Exception:
        pass

    print(f"\nWriting {n_unique:,} rows to staging table …")
    staging_table = None
    written = 0
    for start in range(0, len(rows_to_write), batch_size):
        batch = rows_to_write[start : start + batch_size]
        arrow_batch = _to_arrow_batch(batch, schema)
        if staging_table is None:
            staging_table = db.create_table(STAGING_TABLE, data=arrow_batch, mode="overwrite")
        else:
            staging_table.add(arrow_batch)
        written += len(batch)
        print(f"  written {written:,}/{n_unique:,}", end="\r", flush=True)
    print(f"  written {written:,} rows")

    if staging_table is None:
        raise SystemExit("Nothing was written to staging — aborting")

    print("\nSwapping staging into live …")
    lance_client.promote_staging_table(db, STAGING_TABLE)

    print("Rebuilding search indexes …")
    live = lance_client.docs_table(db)
    lance_client.build_search_indexes(live)

    print(
        f"\nDone. Index reduced from {total_rows:,} to {n_unique:,} rows "
        f"({n_dropped:,} duplicates removed)."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count duplicates, no writes")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    run(dry_run=args.dry_run, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
