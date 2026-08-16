"""
Ingest Aruba/HPE docs into the RAG backend.

Default backend is the embedded stack (no servers): chunk prose -> fastembed
(in-process ONNX, nomic prefixes) -> LanceDB at data/, plus parse OpenAPI
specs -> SQLite (data/specs.sqlite). `--backend redis` keeps the optional
Redis Stack + Ollama server deployment path.

Usage:
    uv run python ingestion/ingest_docs.py                     # full LanceDB rebuild
    uv run python ingestion/ingest_docs.py --backend redis     # optional Redis Stack path
    uv run python ingestion/ingest_docs.py --source nac_docs   # one source only
    uv run python ingestion/ingest_docs.py --dry-run           # count chunks, no upload
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bs4 import BeautifulSoup

from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient
from hpe_networking_mcp.pipeline.clients.redis_client import (
    DOCS_INDEX,
    ensure_index,
    get_client,
    upsert_docs,
)
from ingestion.chunking import chunk_text_with_breadcrumbs

SOURCES_DIR = Path(__file__).parent / "sources"

# Maps source folder name → doc_type tag.
# `doc_type` is kept for back-compat; new code should filter by `source`.
# Some legacy doc_types are intentionally shared across vendor-specific
# sources, so provenance and precise filtering depend on the separate source
# field emitted for every chunk.
SOURCE_META = {
    "devhub": "devhub",
    "developer_docs": "developer-docs",
    "tech_docs": "tech-docs",
    "nac_docs": "nac",
    "vsg_docs": "vsg",
    "techdocs_html": "techdocs-html",
    "feature_navigator": "feature-navigator",
    "openapi_specs": "openapi",
    "product_specs": "product-openapi",
    "aos_techdocs": "aos-techdocs",
    "aoscx_release_notes": "aoscx-release-notes",
    "aoscx_guides": "aoscx-guides",
    "clearpass_guide": "clearpass-guide",
    "security_advisories": "security-advisory",
    "lifecycle_notices": "lifecycle",
    "juniper_lifecycle": "lifecycle",
    "juniper_security_advisories": "security-advisory",
    "juniper_kb": "juniper-kb",
    "mist_docs": "mist-docs",
    "mist_product_updates": "mist-product-updates",
    "junos_ex_hardware": "junos-ex-hardware",
    "junos_ex_release_notes": "junos-ex-release-notes",
    "junos_mx_hardware": "junos-mx-hardware",
    "junos_mx_release_notes": "junos-mx-release-notes",
    "junos_qfx_hardware": "junos-qfx-hardware",
    "junos_qfx_release_notes": "junos-qfx-release-notes",
    "junos_srx_hardware": "junos-srx-hardware",
    "junos_srx_release_notes": "junos-srx-release-notes",
    "product_datasheets": "product-datasheet",
}

# Source folders holding OpenAPI JSON rather than prose. `openapi_specs` is
# Central-only and feeds the exact SQLite spec index; `product_specs` holds the
# other portal products (AOS-CX, ClearPass, EdgeConnect, ...) and is embedded
# for semantic search instead, so it cannot perturb the generated Central client.
OPENAPI_SOURCES = {"openapi_specs", "product_specs"}

UPLOAD_BATCH = 100

# WebHelp books repeat an identical nav/header/footer shell on every topic, and
# it dwarfs the topic itself (a CLI-Bank page is ~1,000 chars of content inside
# ~4,300 chars of chrome). Taking the whole document would put tens of thousands
# of near-identical boilerplate chunks into the index, so prefer the main
# content region when the page marks one.
_HTML_CONTENT_SELECTORS = ("[role=main]", "main", "#mc-main-content", "div.body", "article")

# A region that matches but holds almost nothing means the page uses the
# selector for something else (a nav landing page, a frameset). Falling back to
# the full document is far better than silently indexing an empty topic.
_MIN_MAIN_CHARS = 200


def html_to_text(html: str) -> str:
    """Extract a page's readable text, preferring its main content region."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for selector in _HTML_CONTENT_SELECTORS:
        try:
            region = soup.select_one(selector)
        except Exception:  # noqa: BLE001 - a bad selector must not lose the page
            continue
        if region is None:
            continue
        text = region.get_text(separator="\n")
        if len(text.strip()) >= _MIN_MAIN_CHARS:
            return text
    return soup.get_text(separator="\n")


def read_file(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt"):
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix in (".htm", ".html"):
            return html_to_text(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:
        print(f"  SKIP {path.name}: {e}")
    return None


def _md5_uuid(key: str) -> str:
    """Return a stable UUID string derived from an MD5 hash."""
    return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))


def stable_id(rel_path: str, chunk_index: int) -> str:
    """Derive a stable chunk id from a path already relative to SOURCES_DIR.

    Callers must pass a path resolved relative to ``SOURCES_DIR`` (see
    ``collect_points``), not a raw ``Path`` as constructed from argv/CLI
    invocation — otherwise the same file chunked via a relative vs. an
    absolute invocation path would hash to different ids.
    """
    return _md5_uuid(f"{rel_path}:{chunk_index}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SOURCE_URL_COMMENT_RE = re.compile(r"<!--\s*source:\s*(\S+?)\s*-->")


def extract_source_url(text: str) -> str | None:
    """Extract a leading scraper provenance comment from source text."""
    match = _SOURCE_URL_COMMENT_RE.search(text[:500])
    return match.group(1) if match else None


def _file_source_url(path: Path, chunked_text: str) -> str | None:
    """Recover source URL metadata without changing indexed document text."""
    if path.suffix.lower() not in (".htm", ".html"):
        return extract_source_url(chunked_text)
    try:
        raw_head = path.read_text(encoding="utf-8", errors="ignore")[:500]
    except OSError:
        return None
    return extract_source_url(raw_head)


def _schema_to_text(spec_name: str, schema_name: str, schema: dict) -> str | None:
    """Convert a single OpenAPI schema object to a human-readable text chunk."""
    lines = [f"API spec: {spec_name}", f"Schema: {schema_name}"]

    if desc := schema.get("description"):
        lines.append(f"Description: {desc}")

    props = schema.get("properties", {})
    if not props:
        return None

    field_lines = []
    for field, fdef in props.items():
        # JSON Schema (fully embedded by OpenAPI 3.1) permits boolean schemas
        # (true/false) anywhere a schema is expected, including in
        # properties — skip those rather than crashing the whole run on
        # ``bool.get``.
        if not isinstance(fdef, dict):
            continue
        parts = [f"  - {field}"]
        if fdesc := fdef.get("description"):
            parts.append(f": {fdesc}")
        if ftype := fdef.get("type"):
            parts.append(f" (type: {ftype})")
        if enum_vals := fdef.get("enum"):
            parts.append(f"\n    Valid values: {', '.join(str(v) for v in enum_vals)}")
            if enum_desc := fdef.get("x-enumDescriptions"):
                for val, vdesc in enum_desc.items():
                    parts.append(f"\n      {val}: {vdesc}")
        field_lines.append("".join(parts))

    if not field_lines:
        return None

    lines.append("Fields:")
    lines.extend(field_lines)
    return "\n".join(lines)


def _endpoint_to_text(spec_name: str, path: str, method: str, op: dict) -> str:
    """Convert an OpenAPI path operation to a human-readable text chunk."""
    lines = [
        f"API spec: {spec_name}",
        f"Endpoint: {method.upper()} {path}",
    ]
    if summary := op.get("summary"):
        lines.append(f"Summary: {summary}")
    if desc := op.get("description"):
        lines.append(f"Description: {desc}")
    return "\n".join(lines)


def collect_openapi_points(source_dir: Path, doc_type: str = "openapi") -> list[dict]:
    """Parse OpenAPI JSON specs and emit one chunk per schema and per endpoint."""
    records = []
    files = sorted(source_dir.glob("*.json"))
    print(f"  {source_dir.name}: {len(files)} JSON files")

    for path in files:
        try:
            spec = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            print(f"  SKIP {path.name}: {e}")
            continue

        spec_name = spec.get("info", {}).get("title", path.stem)
        try:
            rel_path = str(path.resolve().relative_to(SOURCES_DIR.resolve()))
        except ValueError:
            # A symlink/mount that resolves outside the sources tree — skip
            # the one file rather than aborting the whole run.
            print(f"  SKIP {path.name}: resolves outside {SOURCES_DIR}")
            continue

        # One chunk per schema
        schemas = spec.get("components", {}).get("schemas", {})
        for schema_name, schema in schemas.items():
            text = _schema_to_text(spec_name, schema_name, schema)
            if not text or not text.strip():
                continue
            chunk_key = f"{rel_path}:schema:{schema_name}"
            records.append(
                {
                    "id": _md5_uuid(chunk_key),
                    "text": text,
                    "source": source_dir.name,
                    "doc_type": doc_type,
                    "file_path": rel_path,
                    "chunk_index": len(records),
                }
            )

        # One chunk per endpoint operation
        for api_path, path_item in spec.get("paths", {}).items():
            for method, op in path_item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                if not isinstance(op, dict):
                    continue
                text = _endpoint_to_text(spec_name, api_path, method, op)
                chunk_key = f"{rel_path}:path:{method}:{api_path}"
                records.append(
                    {
                        "id": _md5_uuid(chunk_key),
                        "text": text,
                        "source": source_dir.name,
                        "doc_type": doc_type,
                        "file_path": rel_path,
                        "chunk_index": len(records),
                    }
                )

    return records


def collect_points(source_dir: Path, doc_type: str) -> list[dict]:
    """Walk source_dir, chunk files, return records without vectors (added later)."""
    if source_dir.name in OPENAPI_SOURCES:
        return collect_openapi_points(source_dir, doc_type)

    records = []
    files = [
        p
        for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".htm", ".html", ".txt")
    ]
    print(f"  {source_dir.name}: {len(files)} files")

    for path in files:
        file_text = read_file(path)
        if not file_text or not file_text.strip():
            continue
        # Resolve both sides before computing the relative path so the id is
        # identical whether ingest_docs.py was invoked with a relative or an
        # absolute path (argv/PYTHONPATH differences otherwise change
        # Path(__file__) and thus SOURCES_DIR's string form).
        try:
            rel_path = str(path.resolve().relative_to(SOURCES_DIR.resolve()))
        except ValueError:
            # A symlink/mount that resolves outside the sources tree — skip
            # the one file rather than aborting the whole run.
            print(f"  SKIP {path.name}: resolves outside {SOURCES_DIR}")
            continue
        source_url = _file_source_url(path, file_text)
        chunks = chunk_text_with_breadcrumbs(file_text)
        for i, (chunk, breadcrumb) in enumerate(chunks):
            records.append(
                {
                    "id": stable_id(rel_path, i),
                    "text": chunk,
                    "source": source_dir.name,
                    "doc_type": doc_type,
                    "file_path": rel_path,
                    "chunk_index": i,
                    "content_hash": content_hash(chunk),
                    "source_url": source_url,
                    "heading_breadcrumb": breadcrumb,
                }
            )
    return records


def _existing_ids(client, ids: list[str]) -> set[str]:
    """Return subset of ids already in Redis."""
    pipe = client.pipeline(transaction=False)
    for doc_id in ids:
        pipe.exists(f"doc:{doc_id}")
    results = pipe.execute()
    return {doc_id for doc_id, exists in zip(ids, results) if exists}


def upload(records: list[dict], ollama: OllamaClient, client):
    skipped = 0
    uploaded = 0
    for batch_start in range(0, len(records), UPLOAD_BATCH):
        batch = records[batch_start : batch_start + UPLOAD_BATCH]
        existing = _existing_ids(client, [r["id"] for r in batch])
        new = [r for r in batch if r["id"] not in existing]
        skipped += len(batch) - len(new)
        if not new:
            continue
        texts = [r["text"] for r in new]
        vectors = ollama.embed_document(texts)
        docs = [{**r, "embedding": vec} for r, vec in zip(new, vectors)]
        upsert_docs(client, docs)
        uploaded += len(new)
        print(f"    uploaded {uploaded} new / {skipped} skipped / {len(records)} total")


WRITE_BATCH_LANCE = 512


def safe_parallel_workers(requested: int | None) -> int | None:
    """Disable fastembed multiprocessing on macOS where forkserver workers
    can deadlock during long index rebuilds."""
    if requested is not None and requested < 1:
        raise ValueError("--parallel must be at least 1")
    if sys.platform == "darwin" and requested is not None:
        print(
            "  macOS detected: disabling fastembed multiprocessing to avoid "
            "forkserver deadlocks; using the in-process embedder.",
            flush=True,
        )
        return None
    return requested


def source_uses_structured_index(folder: str, backend: str) -> bool:
    """OpenAPI stays exact in SQLite for the embedded backend, not vectors."""
    return backend == "lancedb" and folder == "openapi_specs"


def upload_lancedb(
    records: list[dict], ingested_sources: list[str], parallel: int | None = None
) -> None:
    """Full rebuild of the LanceDB docs table: stream embeddings from fastembed
    (one embed pass so parallel workers spawn once), add rows in batches into a
    staging table, assert every ingested source landed >0 chunks (R2 — a
    silently-empty source poisoned the old index), then atomically swap the
    staging table into place and build its FTS index.

    Building into a staging table (rather than overwriting the live "docs"
    table on the first batch) means a crash partway through a large rebuild
    — OOM, disk full, Ctrl-C — leaves the previous good index untouched and
    still serving ``ask_docs``/``search_docs``, instead of a table truncated
    to whatever fraction of the corpus landed before the crash.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    db = lance_client.connect()
    embedder = EmbedClient()
    vectors = embedder.iter_embed_documents(
        (r["text"] for r in records), parallel=safe_parallel_workers(parallel)
    )
    staging_name = f"{lance_client.DOCS_TABLE}__staging"
    table = None
    buf: list[dict] = []
    done = 0
    for record, vec in zip(records, vectors):
        buf.append({**record, "vector": vec})
        if len(buf) >= WRITE_BATCH_LANCE:
            if table is None:
                table = lance_client.create_docs_table(db, buf, table_name=staging_name)
            else:
                table.add(buf)
            done += len(buf)
            buf = []
            print(f"    embedded+added {done}/{len(records)}", flush=True)
    if buf:
        if table is None:
            table = lance_client.create_docs_table(db, buf, table_name=staging_name)
        else:
            table.add(buf)
        done += len(buf)
        print(f"    embedded+added {done}/{len(records)}", flush=True)
    if table is None:
        raise SystemExit("No records to ingest — check ingestion/sources/")

    counts = lance_client.source_counts(db, table_name=staging_name)
    print(f"  per-source counts: {counts}")
    empty = [s for s in ingested_sources if counts.get(s, 0) == 0]
    if empty:
        raise SystemExit(f"FAIL: sources with 0 indexed chunks: {empty}")

    print("  swapping staged index into place...", flush=True)
    live_table = lance_client.promote_staging_table(db, staging_name)
    print("  building FTS index...", flush=True)
    lance_client.build_fts_index(live_table)


def upload_lancedb_incremental(
    records: list[dict],
    ingested_sources: list[str],
    parallel: int | None = None,
) -> bool:
    """Upsert changed chunks and delete removed chunks.

    Returns ``False`` when the existing table predates content hashes so the
    caller can perform one full rebuild and establish the incremental schema.

    ``records`` must reflect a *full-corpus* pass over every source whose
    directory is present on disk this run (the lancedb backend never accepts
    ``--source``; see ``main``'s parser.error). Deletion of stale rows is
    deliberately conservative so an incomplete local ``ingestion/sources``
    tree can never silently wipe an index:

    * A chunk removed from a source that *was* walked this run (its file was
      deleted, or the present source produced fewer/zero chunks) is swept —
      its id is gone from the current full-corpus ``records``.
    * A source folder that is missing this run but is still a known
      ``SOURCE_META`` key is treated as *not downloaded here*, not *deleted*:
      its rows are preserved untouched. This is the common case for a partial
      local checkout and must never trigger a mass delete.
    * Only a source that is no longer in ``SOURCE_META`` at all (truly retired
      from the catalog) has its leftover rows swept even though nothing this
      run names it.

    A run that collects zero records fails closed (raises) rather than
    deleting every row — that is overwhelmingly a missing sources tree, not an
    intentional empty corpus.
    """
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    if not records:
        raise SystemExit(
            "refusing incremental ingest: zero records collected — this is almost "
            "always a missing/undownloaded ingestion/sources tree, not an "
            "intentional empty corpus. The existing docs index was left untouched; "
            "re-run with the source directories present."
        )

    db = lance_client.connect()
    required_columns = {"content_hash", "source_url", "heading_breadcrumb"}
    missing_columns = sorted(required_columns - lance_client.docs_columns(db))
    if missing_columns:
        print(
            f"  existing docs table is missing column(s) {missing_columns}; "
            "performing one full rebuild",
            flush=True,
        )
        return False

    existing_hash: dict[str, str] = {}
    existing_source: dict[str, str] = {}
    for row in lance_client.docs_metadata(db):
        rid = str(row["id"])
        existing_hash[rid] = str(row.get("content_hash") or "")
        existing_source[rid] = str(row.get("source") or "")
    current_ids = {str(record["id"]) for record in records}
    changed = [
        record
        for record in records
        if existing_hash.get(str(record["id"])) != record["content_hash"]
    ]

    # Sources actually touched this run: those explicitly ingested plus any
    # that produced a record. A known-but-missing source dir is in neither, so
    # its rows are preserved; a source dropped from SOURCE_META entirely is
    # "retired" and its leftover rows are swept.
    walked_sources = set(ingested_sources) | {str(record.get("source") or "") for record in records}
    known_sources = set(SOURCE_META)
    removed = sorted(
        rid
        for rid in existing_hash
        if rid not in current_ids
        and (
            existing_source.get(rid, "") in walked_sources
            or existing_source.get(rid, "") not in known_sources
        )
    )
    preserved_absent = sorted(
        {
            existing_source.get(rid, "")
            for rid in existing_hash
            if rid not in current_ids
            and existing_source.get(rid, "") in known_sources
            and existing_source.get(rid, "") not in walked_sources
        }
    )
    if preserved_absent:
        print(
            "  preserving rows for known sources absent this run "
            f"(not downloaded, not deleted): {preserved_absent}",
            flush=True,
        )
    print(
        f"  incremental diff: {len(changed)} changed/new, "
        f"{len(removed)} removed, {len(records) - len(changed)} unchanged",
        flush=True,
    )

    if changed:
        embedder = EmbedClient()
        vectors = embedder.iter_embed_documents(
            (record["text"] for record in changed),
            parallel=safe_parallel_workers(parallel),
        )
        batch: list[dict] = []
        completed = 0
        for record, vector in zip(changed, vectors):
            batch.append({**record, "vector": vector})
            if len(batch) >= WRITE_BATCH_LANCE:
                lance_client.merge_docs_rows(db, batch)
                completed += len(batch)
                batch = []
                print(
                    f"    embedded+merged {completed}/{len(changed)}",
                    flush=True,
                )
        if batch:
            lance_client.merge_docs_rows(db, batch)
            completed += len(batch)
            print(f"    embedded+merged {completed}/{len(changed)}", flush=True)
    if removed:
        lance_client.delete_docs_ids(db, removed)

    table = lance_client.docs_table(db)
    if table is None:
        raise SystemExit("LanceDB docs table disappeared during incremental ingest")
    if changed or removed:
        print("  rebuilding FTS index...", flush=True)
        lance_client.build_fts_index(table)

    counts = lance_client.source_counts(db)
    empty = [source for source in ingested_sources if counts.get(source, 0) == 0]
    if empty:
        raise SystemExit(f"FAIL: sources with 0 indexed chunks: {empty}")
    print(f"  per-source counts: {counts}")
    return True


def _required_sources() -> frozenset[str]:
    """Return source folders required for a safe full prose-index rebuild."""
    from hpe_networking_mcp.pipeline.clients import advisory_index

    return frozenset(advisory_index.SOURCE_DIRS)


def missing_required_sources(sources_dir: Path | None = None) -> list[str]:
    """List required source folders that are absent from the local corpus."""
    base = sources_dir if sources_dir is not None else SOURCES_DIR
    return sorted(name for name in _required_sources() if not (base / name).is_dir())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("lancedb", "redis"), default="lancedb")
    parser.add_argument("--source", help="Ingest one source folder only (redis backend)")
    parser.add_argument("--dry-run", action="store_true", help="Count chunks, no upload")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="upsert changed LanceDB chunks instead of rebuilding every embedding",
    )
    parser.add_argument("--index", default=DOCS_INDEX, dest="index")
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help=("fastembed worker processes (Linux; macOS safely falls back in-process)"),
    )
    args = parser.parse_args()

    if args.backend == "lancedb" and args.source:
        parser.error(
            "--source only applies to --backend redis; lancedb always rebuilds all sources"
        )
    if args.backend == "redis" and args.incremental:
        parser.error("--incremental applies only to the lancedb backend")
    if args.backend == "lancedb" and not args.incremental and not args.dry_run:
        missing = missing_required_sources(SOURCES_DIR)
        if missing:
            raise SystemExit(
                "FAIL: required source folder(s) missing from ingestion/sources/ "
                f"-- refusing to replace the docs index: {missing}. Refresh "
                "them first, or pass --incremental to preserve existing rows."
            )

    sources = {args.source: SOURCE_META.get(args.source, "unknown")} if args.source else SOURCE_META

    all_records: list[dict] = []
    ingested_sources: list[str] = []
    openapi_specs_present = False
    for folder, doc_type in sources.items():
        source_dir = SOURCES_DIR / folder
        if not source_dir.exists():
            print(f"SKIP: {source_dir} not found")
            continue
        records = collect_points(source_dir, doc_type)
        if source_uses_structured_index(folder, args.backend):
            openapi_specs_present = True
            print(f"  → {len(records)} structured API records (SQLite only)")
            continue
        all_records.extend(records)
        ingested_sources.append(folder)
        print(f"  → {len(records)} chunks")

    print(f"\nTotal chunks: {len(all_records)}")

    if args.dry_run:
        print("Dry run — no upload.")
        return

    if args.backend == "lancedb":
        print("\nRebuilding embedded indexes (LanceDB + specs SQLite)...")
        incremental_done = args.incremental and upload_lancedb_incremental(
            all_records,
            ingested_sources,
            parallel=args.parallel,
        )
        if not incremental_done:
            missing = missing_required_sources(SOURCES_DIR)
            if missing:
                raise SystemExit(
                    "FAIL: required source folder(s) missing from "
                    "ingestion/sources/ -- refusing the full rebuild fallback "
                    f"after incremental migration: {missing}. Refresh them "
                    "first, or restore a compatible existing index."
                )
            upload_lancedb(all_records, ingested_sources, parallel=args.parallel)
        if openapi_specs_present:
            from hpe_networking_mcp.pipeline.clients import specs_index

            print("  rebuilding shared structured SQLite index...")
            print(f"  {specs_index.rebuild_shared()}")
    else:
        print("\nConnecting to Redis Stack + Ollama...")
        client = get_client()
        ensure_index(client, args.index)

        with OllamaClient() as ollama:
            print(f"Uploading to index '{args.index}'...")
            upload(all_records, ollama, client)

    print("Done.")


if __name__ == "__main__":
    main()
