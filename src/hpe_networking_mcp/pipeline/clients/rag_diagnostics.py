"""Bounded ingestion-delta and source-freshness diagnostics for RAG.

Three read-only diagnostics, all reusing existing building blocks instead of
reimplementing them:

1. :func:`ingestion_delta` — new/changed/removed/unchanged content-hash
   counts, computed against the current LanceDB ``docs`` table, scoped to
   the four security-advisory/lifecycle source families this v0.7
   workstream owns. Reuses ``ingestion.ingest_docs.collect_points``/
   ``content_hash`` — the exact functions ``ingest_docs.py --incremental``
   uses — purely to *diff*; it never embeds a vector or writes a row (no
   live writes).
2. :func:`full_corpus_delta` — the same bounded diff generalized to every
   LanceDB-vector source family in ``ingest_docs.SOURCE_META``, for callers
   that want corpus-wide staleness rather than just the security/lifecycle
   slice. Shares the exact diff logic with :func:`ingestion_delta` via the
   private ``_content_hash_delta`` helper.
3. :func:`freshness_summary` — reduces the ``source_freshness_result``
   artifact written by ``scripts/check_security_lifecycle_drift.py`` to
   per-status counts plus each source's bounded entry, re-validated through
   ``hpe_networking_mcp.pipeline.artifact_contracts`` so a malformed/stale file is rejected
   loudly instead of silently misread.

None of these functions make a network call, and none expose a raw source
body — only counts, statuses, and the already-bounded ``detail`` strings
the persisted artifact/table already carry.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

ROOT = repo_root()
SOURCES_DIR = ROOT / "ingestion" / "sources"
DEFAULT_FRESHNESS_ARTIFACT = ROOT / "outputs" / "source-freshness.json"
FRESHNESS_MAX_AGE_DAYS = 7


def _ensure_ingestion_importable() -> None:
    """Make the top-level ``ingestion`` package importable regardless of launch.

    ``ingestion/`` lives outside ``src/`` (it is dev/build tooling, not part
    of the installed package), so ``from ingestion import ingest_docs`` only
    succeeds when the repo root happens to already be on ``sys.path``. The
    real MCP router is launched as
    ``python3 src/hpe_networking_mcp/mcp_servers/rag.py`` with
    ``PYTHONPATH=<repo>/src`` only (see ``.cursor/mcp.dev.json``); under that
    launch ``sys.path[0]`` is the script's own directory, not the repo root,
    so the bare import raises ``ModuleNotFoundError`` (reproduced directly).
    Inserting ``repo_root()`` -- already computed via ``Path(__file__)``, so
    it is independent of ``sys.path``/CWD -- fixes this without duplicating
    ``ingest_docs.collect_points``' extraction/hashing logic here. In an
    installed wheel (no ``ingestion/`` directory at all) this is a no-op and
    the subsequent import still raises the same clean ``ModuleNotFoundError``.
    """
    root_str = str(ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

# The source families this diagnostic is scoped to. Kept in sync with
# src/hpe_networking_mcp/pipeline/clients/advisory_index.SOURCE_DIRS, which indexes the same
# folders into the structured advisories/lifecycle_events tables.
DELTA_SOURCE_FAMILIES: tuple[str, ...] = (
    "security_advisories",
    "juniper_security_advisories",
    "lifecycle_notices",
    "juniper_lifecycle",
)

_DOC_TYPE_BY_FAMILY: dict[str, str] = {
    "security_advisories": "security-advisory",
    "juniper_security_advisories": "security-advisory",
    "lifecycle_notices": "lifecycle",
    "juniper_lifecycle": "lifecycle",
}


def ingestion_delta(
    sources: tuple[str, ...] | None = None,
    *,
    sources_dir: Path = SOURCES_DIR,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Return new/changed/removed/unchanged content-hash counts, per source.

    Read-only: computes current chunk content hashes with the same
    ``ingestion.ingest_docs.collect_points``/``content_hash`` helpers the
    incremental ingester uses, and diffs them against the existing LanceDB
    ``docs`` table metadata. Never embeds a vector or writes a row.

    Args:
        sources: Source-family folder names to diff (default: all four
            security/lifecycle families this diagnostic is scoped to).
        sources_dir: Override for ``ingestion/sources`` (tests only). Note
            ``ingest_docs.collect_points`` computes each record's
            ``file_path`` relative to its own module-level
            ``ingest_docs.SOURCES_DIR`` constant, so a test overriding this
            must also monkeypatch that constant to the same root.
        data_dir: Override for the LanceDB ``data/`` directory (tests only).

    Returns:
        ``{"sources": {family: {status, new, changed, removed, unchanged}}}``.
        ``status`` is ``not_yet_indexed`` when the family has zero existing
        rows in the docs table (nothing to diff against yet, not an error),
        or ``missing_source_dir`` when the source folder does not exist.
    """
    families = tuple(sources) if sources else DELTA_SOURCE_FAMILIES
    unknown = [family for family in families if family not in DELTA_SOURCE_FAMILIES]
    if unknown:
        raise ValueError(
            f"unsupported source families for this diagnostic: {unknown}; "
            f"choose from {DELTA_SOURCE_FAMILIES}"
        )

    return _content_hash_delta(
        families, _DOC_TYPE_BY_FAMILY, sources_dir=sources_dir, data_dir=data_dir
    )


def full_corpus_delta(
    *,
    sources_dir: Path = SOURCES_DIR,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Return new/changed/removed/unchanged content-hash counts across every
    known LanceDB-vector source family, not just the four security/lifecycle
    ones :func:`ingestion_delta` is scoped to.

    Generalizes :func:`ingestion_delta` to every folder in
    ``ingestion.ingest_docs.SOURCE_META`` -- the exact catalog a real
    ``ingest_docs.py`` run walks -- minus ``openapi_specs``, which the
    embedded backend indexes as a structured SQLite table rather than
    LanceDB vector chunks (see ``ingest_docs.source_uses_structured_index``)
    and so has no content-hash chunks to diff here.

    Still bounded and read-only, reusing the exact same
    ``collect_points``/``content_hash`` diff as :func:`ingestion_delta`: no
    vector is embedded, no row is written, and only per-source
    counts/status come back, never a raw chunk body. This is a superset of
    :func:`ingestion_delta`'s four families, so calling both is redundant --
    prefer this one when a caller wants corpus-wide staleness rather than
    just the security/lifecycle slice.

    Args:
        sources_dir: Override for ``ingestion/sources`` (tests only). See
            :func:`ingestion_delta` for the same ``ingest_docs.SOURCES_DIR``
            monkeypatch caveat.
        data_dir: Override for the LanceDB ``data/`` directory (tests only).

    Returns:
        Same shape as :func:`ingestion_delta`:
        ``{"sources": {family: {status, new, changed, removed, unchanged}}}``.
    """
    _ensure_ingestion_importable()
    from ingestion import ingest_docs

    families = tuple(
        folder
        for folder in ingest_docs.SOURCE_META
        if not ingest_docs.source_uses_structured_index(folder, "lancedb")
    )
    doc_type_by_family = {folder: ingest_docs.SOURCE_META[folder] for folder in families}

    return _content_hash_delta(
        families, doc_type_by_family, sources_dir=sources_dir, data_dir=data_dir
    )


def _content_hash_delta(
    families: tuple[str, ...],
    doc_type_by_family: dict[str, str],
    *,
    sources_dir: Path,
    data_dir: Path | None,
) -> dict[str, Any]:
    """Shared new/changed/removed/unchanged content-hash diff.

    Factored out of :func:`ingestion_delta` so :func:`full_corpus_delta` can
    reuse the identical, already-reviewed diff logic over a larger family
    set instead of duplicating it.
    """
    _ensure_ingestion_importable()
    from hpe_networking_mcp.pipeline.clients import lance_client
    from ingestion import ingest_docs

    connect_kwargs = {} if data_dir is None else {"data_dir": data_dir}
    db = lance_client.connect(**connect_kwargs)

    existing_by_source: dict[str, dict[str, str]] = {}
    table_exists = lance_client.docs_table(db) is not None
    has_content_hash = "content_hash" in lance_client.docs_columns(db)
    legacy_table = table_exists and not has_content_hash
    if has_content_hash:
        for row in lance_client.docs_metadata(db):
            existing_by_source.setdefault(row.get("source", ""), {})[str(row["id"])] = str(
                row.get("content_hash") or ""
            )

    results: dict[str, Any] = {}
    for family in families:
        source_dir = sources_dir / family
        if not source_dir.exists():
            results[family] = {
                "status": "missing_source_dir",
                "new": 0,
                "changed": 0,
                "removed": 0,
                "unchanged": 0,
            }
            continue

        # collect_points prints progress lines; suppress them so this stays
        # safe to call from a stdio-transport MCP tool (stdout carries the
        # JSON-RPC stream there).
        with contextlib.redirect_stdout(io.StringIO()):
            records = ingest_docs.collect_points(source_dir, doc_type_by_family[family])

        current = {str(record["id"]): str(record["content_hash"]) for record in records}
        if legacy_table:
            results[family] = {
                "status": "full_rebuild_required",
                "new": len(current),
                "changed": 0,
                "removed": 0,
                "unchanged": 0,
            }
            continue
        existing = existing_by_source.get(family, {})
        new_count = sum(1 for doc_id in current if doc_id not in existing)
        changed_count = sum(
            1
            for doc_id, digest in current.items()
            if doc_id in existing and existing[doc_id] != digest
        )
        removed_count = sum(1 for doc_id in existing if doc_id not in current)
        unchanged_count = len(current) - new_count - changed_count
        results[family] = {
            "status": "indexed" if existing else "not_yet_indexed",
            "new": new_count,
            "changed": changed_count,
            "removed": removed_count,
            "unchanged": unchanged_count,
        }

    return {"sources": results}


def freshness_summary(
    artifact_path: Path = DEFAULT_FRESHNESS_ARTIFACT,
) -> dict[str, Any]:
    """Reduce the persisted ``source_freshness_result`` artifact to status counts.

    Re-validates the artifact through ``hpe_networking_mcp.pipeline.artifact_contracts`` so a
    malformed or schema-mismatched file is rejected loudly instead of
    silently misread. Returns per-status counts plus each source's bounded
    entry (``source``, ``count``, ``minimum``, ``status``, ``detail``) —
    never a raw source body.

    Raises:
        FileNotFoundError: no artifact has been generated yet — run
            ``scripts/check_security_lifecycle_drift.py`` to create one.
        contracts.ArtifactValidationError: the file exists but fails schema
            validation (stale/incompatible shape).
    """
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"No source-freshness artifact at {artifact_path}; run "
            "scripts/check_security_lifecycle_drift.py to generate one"
        )
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    snapshot = contracts.build_artifact(contracts.SOURCE_FRESHNESS_RESULT, payload)
    data = contracts.to_json_dict(snapshot)

    status_counts: dict[str, int] = {}
    for entry in data["entries"]:
        status_counts[entry["status"]] = status_counts.get(entry["status"], 0) + 1

    generated = datetime.fromisoformat(str(data["generated_at"]))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age_days = max(0, (datetime.now(timezone.utc) - generated).days)

    return {
        "generated_at": data["generated_at"],
        "schema_version": data["schema_version"],
        "status_counts": status_counts,
        "entries": data["entries"],
        "age_days": age_days,
        "artifact_stale": age_days >= FRESHNESS_MAX_AGE_DAYS,
    }
