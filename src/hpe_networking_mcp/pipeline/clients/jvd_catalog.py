"""Local, deterministic Juniper Validated Designs (JVD) structured index.

This module mirrors the official Juniper/jvd GitHub repository's portal
catalog (``portal/src/data/jvds.json``) into a local SQLite index built from
a reviewed, commit-pinned JSON snapshot. It intentionally keeps validated
design discovery out of prose RAG: an operator choosing a design pattern
needs deterministic area/platform/OS matches and an exact source path back
to the pinned JVD commit, not semantically similar documentation chunks.

Every design record carries its official JVD repository path and the
pinned commit SHA used for the snapshot, so a result can always be traced to
exact JVD content. There is no runtime network dependency: the index is
built by ``scripts/build_jvd_index.py`` from the committed
``ingestion/jvd_seed.json`` snapshot, following the same official-source,
retain-last-verified, mark-stale policy as the hardware catalog
(``ingestion/jvd_manifest.json``).

This is a design/BOM *discovery* aid, not a config generator: it never
renders or pushes device configuration. Use it to find which JVD applies to
a requirement and where its README, configuration tree, and snips live in
the official repository, before drawing a topology or examining the JVD's
own configuration artifacts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

ROOT = repo_root()
SEED_PATH = ROOT / "ingestion" / "jvd_seed.json"
DB_PATH = ROOT / "data" / "jvd_index.sqlite"
BUILD_COMMAND = "python scripts/build_jvd_index.py"
MISSING_INDEX_REMEDY = (
    f"build the local JVD index with `{BUILD_COMMAND}`; it uses the committed "
    "official-source snapshot and does not make a network call at query time"
)

_SCHEMA = """
CREATE TABLE designs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    area TEXT NOT NULL,
    description TEXT NOT NULL,
    platforms_json TEXT NOT NULL,
    os_json TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE VIRTUAL TABLE design_fts USING fts5(
    id, name, area, description, platforms, os,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE INDEX idx_designs_area ON designs(area);
"""


def _missing_index_error(db_path: Path | str) -> FileNotFoundError:
    return FileNotFoundError(f"JVD index missing at {db_path} — {MISSING_INDEX_REMEDY}")


def _is_valid_design(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    required = ("id", "name", "area", "description", "repo_path", "source_url")
    if not all(isinstance(record.get(key), str) and record[key].strip() for key in required):
        return False
    if not isinstance(record.get("platforms"), list) or not record["platforms"]:
        return False
    if not isinstance(record.get("os"), list) or not record["os"]:
        return False
    return str(record["source_url"]).startswith("https://github.com/Juniper/jvd/")


def _load_seed(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JVD seed {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"JVD seed {path} must have schema_version 1")
    if not isinstance(data.get("source_commit"), str) or not data["source_commit"].strip():
        raise ValueError(f"JVD seed {path} must pin a source_commit")
    designs = data.get("designs")
    if not isinstance(designs, list) or not designs:
        raise ValueError(f"JVD seed {path} has no designs")
    invalid = [index for index, record in enumerate(designs) if not _is_valid_design(record)]
    if invalid:
        raise ValueError(f"JVD seed {path} has invalid records at indexes {invalid[:5]}")
    return data


def build(*, seed_path: Path = SEED_PATH, db_path: Path = DB_PATH) -> dict[str, int]:
    """Build a deterministic JVD design index from a reviewed JSON snapshot."""
    seed = _load_seed(Path(seed_path))
    designs = seed["designs"]
    output = Path(db_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    seen_ids: set[str] = set()
    try:
        conn = sqlite3.connect(temporary)
        try:
            conn.executescript(_SCHEMA)
            for record in sorted(designs, key=lambda item: str(item["id"])):
                design_id = str(record["id"]).strip()
                if design_id in seen_ids:
                    raise ValueError(f"duplicate JVD design id {design_id!r}")
                seen_ids.add(design_id)
                platforms = [str(p) for p in record["platforms"]]
                os_list = [str(o) for o in record["os"]]
                conn.execute(
                    "INSERT INTO designs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        design_id,
                        str(record["name"]).strip(),
                        str(record["area"]).strip(),
                        str(record["description"]).strip(),
                        json.dumps(platforms, sort_keys=True),
                        json.dumps(os_list, sort_keys=True),
                        str(record["repo_path"]).strip(),
                        str(record["source_url"]).strip(),
                    ),
                )
                conn.execute(
                    "INSERT INTO design_fts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        design_id,
                        str(record["name"]).strip(),
                        str(record["area"]).strip(),
                        str(record["description"]).strip(),
                        " ".join(platforms),
                        " ".join(os_list),
                    ),
                )
            metadata = {
                "schema_version": "1",
                "source_repo": str(seed.get("source_repo") or ""),
                "source_commit": str(seed["source_commit"]),
                "source_license": str(seed.get("source_license") or ""),
                "coverage": str(seed.get("coverage") or "partial"),
                "coverage_note": str(seed.get("coverage_note") or ""),
                "snapshot_at": str(seed.get("snapshot_at") or ""),
                "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            conn.executemany("INSERT INTO index_meta(key, value) VALUES (?, ?)", metadata.items())
            conn.commit()
        finally:
            conn.close()
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"designs": len(seen_ids)}


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise _missing_index_error(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM index_meta")
    }


def _as_result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "area": row["area"],
        "description": row["description"],
        "platforms": json.loads(row["platforms_json"]),
        "os": json.loads(row["os_json"]),
        "source": {
            "repo_path": row["repo_path"],
            "url": row["source_url"],
        },
    }


def get_design(design_id: str, *, db_path: Path = DB_PATH) -> dict[str, Any]:
    """Look up exactly one JVD design by its stable id.

    Unlike :func:`search_designs`, this never falls back to fuzzy or
    area-only matching: callers that already know the id (for example, from
    a prior ``search_designs`` result) get a deterministic, unambiguous
    reference back to the pinned JVD commit, or an explicit not-found error.
    """
    clean_id = design_id.strip()
    if not clean_id:
        return {"ok": False, "error": "design_id must not be empty"}
    try:
        conn = _connect(Path(db_path))
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "hint": MISSING_INDEX_REMEDY}
    try:
        metadata = _metadata(conn)
        provenance = {
            "identity_authority": "official_jvd_repository",
            "source_repo": metadata.get("source_repo", ""),
            "source_commit": metadata.get("source_commit", ""),
            "source_license": metadata.get("source_license", ""),
            "coverage": metadata.get("coverage", "partial"),
            "coverage_note": metadata.get("coverage_note", ""),
        }
        row = conn.execute("SELECT * FROM designs WHERE id = ?", [clean_id]).fetchone()
        if row is None:
            return {
                "ok": False,
                "match_type": "no_match",
                "error": f"no JVD design with id {clean_id!r}",
                "guidance": "Use search_designs to find a valid id before calling get_design.",
                "provenance": provenance,
            }
        return {
            "ok": True,
            "match_type": "exact_id",
            "result": _as_result(row),
            "provenance": provenance,
        }
    finally:
        conn.close()


def search_designs(
    query: str,
    *,
    area: str | None = None,
    limit: int = 5,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Find JVD validated designs by area, platform, use case, or free text.

    Returns bounded, ranked matches with the exact JVD repository path and a
    provenance block pinning the source commit. This never renders a
    configuration; it only locates the applicable validated design.
    """
    clean_query = query.strip()
    try:
        conn = _connect(Path(db_path))
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "hint": MISSING_INDEX_REMEDY}
    try:
        metadata = _metadata(conn)
        provenance = {
            "identity_authority": "official_jvd_repository",
            "source_repo": metadata.get("source_repo", ""),
            "source_commit": metadata.get("source_commit", ""),
            "source_license": metadata.get("source_license", ""),
            "coverage": metadata.get("coverage", "partial"),
            "coverage_note": metadata.get("coverage_note", ""),
        }
        try:
            bounded_limit = max(1, min(int(limit), 5))
        except (TypeError, ValueError):
            bounded_limit = 5
        where_clauses = []
        params: list[Any] = []
        if area:
            where_clauses.append("area = ?")
            params.append(area)
        rows: list[sqlite3.Row]
        if clean_query:
            match = " OR ".join(f'"{tok}"' for tok in clean_query.casefold().split()[:16])
            sql = (
                "SELECT d.* FROM design_fts f JOIN designs d ON d.id = f.id "
                "WHERE design_fts MATCH ?"
            )
            fts_params: list[Any] = [match]
            if area:
                sql += " AND d.area = ?"
                fts_params.append(area)
            try:
                rows = conn.execute(sql, fts_params).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            base_sql = "SELECT * FROM designs"
            if where_clauses:
                base_sql += " WHERE " + " AND ".join(where_clauses)
            rows = conn.execute(base_sql, params).fetchall()
        if not rows and clean_query and area:
            # Fall back to area-only match when the free-text query is too
            # narrow, keeping area filtering the stronger, deterministic signal.
            rows = conn.execute("SELECT * FROM designs WHERE area = ?", [area]).fetchall()
        results = [_as_result(row) for row in rows[:bounded_limit]]
        if not results:
            return {
                "ok": False,
                "match_type": "no_match",
                "results": [],
                "guidance": (
                    "No JVD match. Include the design area (for example Data Center, "
                    "Enterprise WAN, Security, Service Provider) or a platform/use-case "
                    "keyword from the JVD catalog."
                ),
                "provenance": provenance,
            }
        return {
            "ok": True,
            "match_type": "candidate" if len(results) > 1 else "best_match",
            "results": results,
            "provenance": provenance,
        }
    finally:
        conn.close()
