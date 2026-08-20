"""SQLite structured index over the Aruba OpenAPI specs — exact API lookup.

Vector search is the wrong tool for "what enum values does field X accept" or
"which endpoint configures Y": those need lossless, authoritative answers.
This module parses ingestion/sources/openapi_specs/*.json into SQLite with
FTS5 keyword search, giving exact endpoint / schema / field / enum lookup.
Stdlib only — no new dependencies. See docs/architecture/RAG-ARCHITECTURE.md.

Build:   python -m hpe_networking_mcp.pipeline.clients.specs_index --build
Query:   python -m hpe_networking_mcp.pipeline.clients.specs_index --query "auth-type"
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

# See lance_client.ROOT: ``parents[2]`` is the package dir under src/ layout,
# so the structured spec index was looked up at a path that never exists.
ROOT = repo_root()
SPECS_DIR = ROOT / "ingestion" / "sources" / "openapi_specs"
DB_PATH = ROOT / "data" / "specs.sqlite"
_FULL_REBUILD_COMMAND = (
    "uv run python -m hpe_networking_mcp.pipeline.clients.specs_index --rebuild-shared"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY,
    spec_name TEXT, spec_file TEXT, server TEXT,
    method TEXT, path TEXT, operation_id TEXT, summary TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS schemas (
    id INTEGER PRIMARY KEY,
    spec_name TEXT, spec_file TEXT, name TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY,
    spec_name TEXT, spec_file TEXT, schema_name TEXT,
    field_name TEXT, path TEXT, type TEXT, description TEXT,
    enums TEXT, enum_descriptions TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    kind, spec_file, ref, body
);
CREATE INDEX IF NOT EXISTS idx_fields_name ON fields(field_name);
CREATE INDEX IF NOT EXISTS idx_fields_schema ON fields(schema_name);
CREATE INDEX IF NOT EXISTS idx_endpoints_path ON endpoints(path);
CREATE INDEX IF NOT EXISTS idx_endpoints_operation_id
    ON endpoints(operation_id COLLATE NOCASE);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _walk_fields(node: Any, path: str, depth: int = 0):
    """Recursively yield (field_path, field_name, fdef) from a schema node.

    Fields hide inside items/allOf/anyOf/oneOf nesting (e.g.
    properties/profile/items/allOf[8]/properties/auth-type), so a
    top-level-properties walk misses most of them.
    """
    if depth > 12 or not isinstance(node, dict):
        return
    for field, fdef in (node.get("properties") or {}).items():
        if not isinstance(fdef, dict):
            continue
        fpath = f"{path}.{field}" if path else field
        yield fpath, field, fdef
        yield from _walk_fields(fdef, fpath, depth + 1)
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_fields(items, f"{path}[]", depth + 1)
    for comb in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(comb) or []:
            yield from _walk_fields(sub, path, depth + 1)


def _populate_openapi_tables(
    conn: sqlite3.Connection,
    specs_dir: Path,
) -> dict[str, int]:
    """Populate OpenAPI-owned tables in an initialized SQLite connection."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS endpoints;
        DROP TABLE IF EXISTS schemas;
        DROP TABLE IF EXISTS fields;
        DROP TABLE IF EXISTS fts;
        """
    )
    conn.executescript(_SCHEMA)

    counts = {"specs": 0, "endpoints": 0, "schemas": 0, "fields": 0, "skipped": 0}
    for path in sorted(specs_dir.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            counts["skipped"] += 1
            continue
        counts["specs"] += 1
        spec_name = spec.get("info", {}).get("title", path.stem)
        spec_file = path.name
        servers = spec.get("servers") or []
        server = servers[0].get("url", "") if servers else ""

        for api_path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                    continue
                summary = op.get("summary", "")
                desc = op.get("description", "")
                conn.execute(
                    "INSERT INTO endpoints "
                    "(spec_name, spec_file, server, method, path, operation_id, "
                    "summary, description) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        spec_name, spec_file, server, method.upper(), api_path,
                        op.get("operationId", ""), summary, desc,
                    ),
                )
                conn.execute(
                    "INSERT INTO fts (kind, spec_file, ref, body) VALUES (?,?,?,?)",
                    ("endpoint", spec_file, f"{method.upper()} {api_path}",
                     f"{spec_name} {api_path} {summary} {desc}"),
                )
                counts["endpoints"] += 1

        for schema_name, schema in (spec.get("components", {}).get("schemas") or {}).items():
            if not isinstance(schema, dict):
                continue
            s_desc = schema.get("description", "")
            conn.execute(
                "INSERT INTO schemas (spec_name, spec_file, name, description) VALUES (?,?,?,?)",
                (spec_name, spec_file, schema_name, s_desc),
            )
            counts["schemas"] += 1
            prop_texts = []
            for fpath, field, fdef in _walk_fields(schema, ""):
                enums = fdef.get("enum")
                enum_desc = fdef.get("x-enumDescriptions")
                conn.execute(
                    "INSERT INTO fields (spec_name, spec_file, schema_name, field_name, path, type, description, enums, enum_descriptions) VALUES (?,?,?,?,?,?,?,?,?)",
                    (spec_name, spec_file, schema_name, field, fpath,
                     str(fdef.get("type", "")), fdef.get("description", ""),
                     json.dumps(enums) if enums else None,
                     json.dumps(enum_desc) if enum_desc else None),
                )
                counts["fields"] += 1
                if enums or fdef.get("description"):
                    prop_texts.append(f"{field} {fdef.get('description','')} {' '.join(map(str, enums or []))}")
            conn.execute(
                "INSERT INTO fts (kind, spec_file, ref, body) VALUES (?,?,?,?)",
                ("schema", spec_file, schema_name,
                 f"{spec_name} {schema_name} {s_desc} {' '.join(prop_texts)}"),
            )
    return counts


def _check_integrity(conn: sqlite3.Connection, db_path: Path) -> None:
    try:
        result = [row[0] for row in conn.execute("PRAGMA quick_check(1)").fetchall()]
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"shared index at {db_path} is corrupt; rebuild it with "
            f"`{_FULL_REBUILD_COMMAND}`"
        ) from exc
    if result != ["ok"]:
        raise RuntimeError(
            f"shared index at {db_path} failed integrity checking: {result!r}; "
            f"rebuild it with `{_FULL_REBUILD_COMMAND}`"
        )


def build(
    specs_dir: Path = SPECS_DIR,
    db_path: Path = DB_PATH,
    *,
    preserve_shared: bool = True,
) -> dict[str, int]:
    """Parse all OpenAPI specs into the shared SQLite index.

    Builds into a sibling temp file and atomically replaces the live path only
    after a full commit. Existing non-OpenAPI tables are copied forward by
    default because advisory and lifecycle indexes share this artifact. Full
    ingestion and ``--rebuild-shared`` set ``preserve_shared=False`` because
    they rebuild every shared table.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    conn = connect(tmp_path)
    try:
        if preserve_shared and db_path.exists():
            source = connect(db_path)
            try:
                _check_integrity(source, db_path)
                source.backup(conn)
            finally:
                source.close()
        counts = _populate_openapi_tables(conn, specs_dir)
        if counts["specs"] <= 0 or counts["endpoints"] <= 0:
            raise RuntimeError(
                f"no OpenAPI records found under {specs_dir}; refresh the "
                "git-ignored ingestion sources before rebuilding"
            )
        conn.commit()
    except Exception:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    try:
        os.replace(tmp_path, db_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return counts


def _validate_knowledge_sources(
    sources_dir: Path,
    source_families: list[str],
) -> None:
    missing: list[str] = []
    for source_family in source_families:
        source_dir = sources_dir / source_family
        readable = False
        if source_dir.is_dir():
            for path in sorted(source_dir.rglob("*.md")):
                try:
                    if path.read_text(encoding="utf-8", errors="ignore").strip():
                        readable = True
                        break
                except OSError:
                    continue
        if not readable:
            missing.append(source_family)
    if missing:
        raise RuntimeError(
            "missing or empty structured source families: "
            f"{', '.join(missing)}; refresh the git-ignored ingestion sources "
            "before rebuilding"
        )


def rebuild_shared(
    db_path: Path = DB_PATH,
    sources_dir: Path | None = None,
) -> dict[str, dict[str, int]]:
    """Rebuild every table in the shared structured SQLite artifact."""
    from hpe_networking_mcp.pipeline.clients import advisory_index

    knowledge_sources = sources_dir or advisory_index.SOURCES_DIR
    _validate_knowledge_sources(
        knowledge_sources,
        list(advisory_index.SOURCE_DIRS),
    )
    staging_path = db_path.with_name(db_path.name + ".shared.tmp")
    staging_path.unlink(missing_ok=True)
    try:
        openapi_counts = build(db_path=staging_path, preserve_shared=False)
        knowledge_counts = advisory_index.build(
            sources_dir=knowledge_sources,
            db_path=staging_path,
        )
        if (
            knowledge_counts.get("advisories", 0) <= 0
            or knowledge_counts.get("lifecycle_events", 0) <= 0
        ):
            raise RuntimeError(
                "no advisory or lifecycle records were indexed; refresh the "
                "git-ignored ingestion sources before rebuilding"
            )
        result = {"openapi": openapi_counts, "knowledge": knowledge_counts}
        os.replace(staging_path, db_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return result


# ---------------------------------------------------------------------------
# Query helpers (used by the lookup_api MCP tool)
# ---------------------------------------------------------------------------

def _fts_escape(q: str) -> str:
    """Quote each term so FTS5 treats hyphens/slashes literally."""
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def search(query: str, kind: str | None = None, limit: int = 10,
           db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """FTS keyword search across endpoints + schemas. kind: endpoint|schema."""
    conn = connect(db_path)
    sql = "SELECT kind, spec_file, ref, snippet(fts, 3, '', '', '…', 24) AS snippet FROM fts WHERE fts MATCH ?"
    params: list[Any] = [_fts_escape(query)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_endpoint(path_contains: str, method: str | None = None,
                 limit: int = 10, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Exact-ish endpoint lookup by path substring (and optional method)."""
    conn = connect(db_path)
    sql = "SELECT spec_name, spec_file, server, method, path, summary, description FROM endpoints WHERE path LIKE ?"
    params: list[Any] = [f"%{path_contains}%"]
    if method:
        sql += " AND method = ?"
        params.append(method.upper())
    sql += " LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_exact_endpoint(method: str, path: str, limit: int = 10,
                       db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Exact endpoint lookup by HTTP method and literal OpenAPI path."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT spec_name, spec_file, server, method, path, summary, description "
            "FROM endpoints WHERE method = ? AND path = ? "
            "ORDER BY spec_file, id LIMIT ?",
            (method.upper(), path, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_endpoint_by_operation_id(operation_id: str, limit: int = 10,
                                 db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Exact case-insensitive endpoint lookup by OpenAPI operationId."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT spec_name, spec_file, server, method, path, summary, "
            "description FROM endpoints "
            "WHERE operation_id = ? COLLATE NOCASE "
            "ORDER BY spec_file, id LIMIT ?",
            (operation_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_schema(name_contains: str, limit: int = 5,
               db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Schema lookup with its full field list (types, enums)."""
    conn = connect(db_path)
    try:
        schemas = conn.execute(
            "SELECT spec_name, spec_file, name, description FROM schemas WHERE name LIKE ? LIMIT ?",
            (f"%{name_contains}%", limit),
        ).fetchall()
        out = []
        for s in schemas:
            fields = conn.execute(
                "SELECT field_name, path, type, description, enums, enum_descriptions FROM fields WHERE schema_name = ? AND spec_file = ?",
                (s["name"], s["spec_file"]),
            ).fetchall()
            out.append({
                **dict(s),
                "fields": [
                    {**dict(f),
                     "enums": json.loads(f["enums"]) if f["enums"] else None,
                     "enum_descriptions": json.loads(f["enum_descriptions"]) if f["enum_descriptions"] else None}
                    for f in fields
                ],
            })
    finally:
        conn.close()
    return out


def get_enum(field_name: str, schema_contains: str | None = None,
             limit: int = 10, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Authoritative enum values for a field, across all specs."""
    conn = connect(db_path)
    sql = ("SELECT spec_name, spec_file, schema_name, field_name, path, type, description, enums, enum_descriptions "
           "FROM fields WHERE field_name = ? AND enums IS NOT NULL")
    params: list[Any] = [field_name]
    if schema_contains:
        sql += " AND schema_name LIKE ?"
        params.append(f"%{schema_contains}%")
    sql += " LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {**dict(r),
         "enums": json.loads(r["enums"]),
         "enum_descriptions": json.loads(r["enum_descriptions"]) if r["enum_descriptions"] else None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# High-level natural-language lookup (backs the lookup_api MCP tool)
# ---------------------------------------------------------------------------

# Question scaffolding + terms so generic in an API-spec corpus they only add
# noise ("config", "endpoint", "value" appear in nearly every row).
_STOPWORDS = frozenset("""
a an the is are was were be been being do does did can could should would will
what which who whose when where why how there here this that these those it its
i you we they my your of for to in on at by with from into over under about as
and or not no if then than but exist exists existing available use used uses
using new central api apis valid value values field fields enum enums key keys
required need needed needs http https method methods url uri endpoint endpoints
response request list lists get read set sets type types kind name names
configure configures configured configuration config
accept accepts allow allows allowed support supports supported
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-._]*")
_EXACT_ENDPOINT_RE = re.compile(
    r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s?#]*)\s*$",
    re.IGNORECASE,
)
_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{1,255}$")
_LOOKUP_CACHE_MAX = 128
_lookup_cache: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()

# Tight, curated domain synonyms — Aruba docs use these interchangeably
# (specs say "wlan"/"essid" where users say "SSID"). Synonyms join the SAME
# concept group, so they corroborate a match without inflating the
# distinct-concept count that the relevance threshold counts.
_SYNONYMS: dict[str, list[str]] = {
    "ssid": ["wlan", "essid"],
    "wlan": ["ssid", "essid"],
    "gw": ["gateway"],
    "gateway": ["gw"],
}


def _stem_variants(word: str) -> list[str]:
    """Singular-fold variants of a word. "policies" needs both spellings since
    neither "policy" nor "policie" alone covers the other as a token prefix;
    a plain trailing-s plural folds to one prefix-safe stem."""
    if word.endswith("ies") and len(word) > 4:
        return [word[:-3] + "y", word[:-1]]
    if word.endswith("s") and len(word) > 3:
        return [word[:-1]]
    return [word]


def _query_groups(query: str) -> list[list[str]]:
    """Concept groups of lightly-stemmed terms from a natural-language query.

    Each non-stopword token becomes ONE group holding the whole token plus, for
    hyphenated tokens, its components ("device-firmware-upgrade" also yields
    device/firmware/upgrade for recall when the corpus spells it differently).
    Scoring counts GROUPS hit, not raw stems — otherwise a single hyphenated
    concept would corroborate itself through its own components and defeat the
    relevance threshold.
    """
    groups: list[list[str]] = []
    seen_tokens: set[str] = set()
    for tok in _TOKEN_RE.findall(query.lower()):
        tok = tok.strip("-._")
        if not tok or tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        stems: list[str] = []
        for part in [tok] + (tok.split("-") if "-" in tok else []):
            # Check the RAW word against stopwords too — stemming first would
            # let scaffolding sneak through ("does" -> "doe" is not a stopword).
            if part in _STOPWORDS:
                continue
            for v in _stem_variants(part):
                if len(v) < 3 or v.isdigit() or v in _STOPWORDS or v in stems:
                    continue
                stems.append(v)
        for s in list(stems):
            for syn in _SYNONYMS.get(s, []):
                if syn not in stems:
                    stems.append(syn)
        if stems:
            groups.append(stems)
    return groups


def _fmt_endpoint(row: dict[str, Any]) -> str:
    url = f"{row['server']}{row['path']}" if row.get("server") else row["path"]
    desc = (row.get("description") or "")[:300]
    return f"{row['method']} {url} — {row.get('summary', '')} {desc}".strip()


def _exact_endpoint_hits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "text": _fmt_endpoint(row),
            "source": "openapi_specs",
            "file_path": (
                f"openapi_specs/{row['spec_file']}#{row['method']} {row['path']}"
            ),
            "kind": "endpoint",
            "score": 100,
        }
        for row in rows
    ]


def _fmt_enum_field(row: dict[str, Any]) -> str:
    enums = row.get("enums") or []
    text = (f"{row['schema_name']}.{row['path']} ({row['spec_name']}) "
            f"type={row.get('type', '')}: {(row.get('description') or '')[:200]} "
            f"Enum: {', '.join(map(str, enums[:24]))}")
    return text.strip()


def lookup(query: str, top_k: int = 10, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Exact API lookup for a natural-language question. Returns [] when the
    specs have no confident answer (caller should fall back to prose search).

    Four strategies:
      1. exact HTTP method/path or operationId equality lookup
      2. exact enum/field match for field-like terms (get_enum)
      3. exact endpoint match for hyphenated path-like tokens, with one
         progressive right-trim (device-firmware-upgrade -> device-firmware)
      4. FTS prefix search re-ranked by how many distinct query terms the row
         actually contains (bm25 alone ranks short generic rows too high)

    Hits are {"text", "source", "file_path", "kind", "score"} — file_path is a
    precise locator (openapi_specs/<spec_file>#<ref>) for source attribution.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"specs index missing at {db_path} — build it with "
            "`python -m hpe_networking_mcp.pipeline.clients.specs_index --build`"
        )
    top_k = max(1, min(20, top_k))
    try:
        return _cached_lookup(query, top_k, db_path)
    except sqlite3.Error as exc:
        # A present-but-unreadable DB (corrupt file, or the empty/schemaless
        # window an interrupted --build leaves behind) must not crash the MCP
        # tool — surface it the same way as a missing index.
        raise FileNotFoundError(
            f"specs index at {db_path} is unreadable ({exc}) — rebuild it with "
            f"`{_FULL_REBUILD_COMMAND}`"
        ) from exc


def _db_fingerprint(db_path: Path) -> tuple[int, int]:
    stat = Path(db_path).stat()
    return int(stat.st_mtime_ns), int(stat.st_size)


def clear_lookup_cache() -> None:
    """Drop the process-local lookup cache (tests / index rebuilds)."""
    _lookup_cache.clear()


def _cached_lookup(query: str, top_k: int, db_path: Path) -> list[dict[str, Any]]:
    key = (str(db_path), query.strip(), top_k, _db_fingerprint(db_path))
    cached = _lookup_cache.get(key)
    if cached is not None:
        _lookup_cache.move_to_end(key)
        return [dict(hit) for hit in cached]
    hits = _lookup(query, top_k, db_path)
    _lookup_cache[key] = [dict(hit) for hit in hits]
    _lookup_cache.move_to_end(key)
    while len(_lookup_cache) > _LOOKUP_CACHE_MAX:
        _lookup_cache.popitem(last=False)
    return [dict(hit) for hit in hits]


def _lookup(query: str, top_k: int, db_path: Path) -> list[dict[str, Any]]:
    stripped = query.strip()
    exact_endpoint = _EXACT_ENDPOINT_RE.fullmatch(stripped)
    if exact_endpoint:
        method, path = exact_endpoint.groups()
        return _exact_endpoint_hits(
            get_exact_endpoint(method, path, limit=top_k, db_path=db_path)
        )
    if _OPERATION_ID_RE.fullmatch(stripped):
        operation_rows = get_endpoint_by_operation_id(
            stripped, limit=top_k, db_path=db_path
        )
        if operation_rows:
            return _exact_endpoint_hits(operation_rows)

    groups = _query_groups(query)
    if not groups:
        return []
    n_terms = len(groups)
    threshold = 1 if n_terms == 1 else (2 if n_terms <= 3 else 3)
    flat: list[str] = []
    for g in groups:
        for s in g:
            if s not in flat:
                flat.append(s)

    def _present(stem: str, low: str) -> bool:
        # Short stems are substring-fragile ("mac" is inside "machine") —
        # require a full word. Longer stems keep substring semantics so
        # "layer2" finds Layer2VlanSchema and "personal" finds WPA2_PERSONAL.
        if len(stem) <= 3:
            return bool(re.search(rf"\b{re.escape(stem)}\b", low))
        return stem in low

    def matched(blob: str) -> int:
        low = blob.lower()
        return sum(1 for g in groups if any(_present(s, low) for s in g))

    hits: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, spec_file: str, ref: str, text: str, score: int, exact: bool) -> None:
        cur = hits.get((spec_file, ref))
        if cur is None:
            hits[(spec_file, ref)] = {
                "text": text,
                "source": "openapi_specs",
                "file_path": f"openapi_specs/{spec_file}#{ref}",
                "kind": kind,
                "score": score,
                "_exact": exact,
            }
        else:
            # Same row reached by two strategies: keep the best evidence of each
            cur["score"] = max(cur["score"], score)
            cur["_exact"] = cur["_exact"] or exact

    # 1. Exact enum/field lookups for field-like terms. Generous limit — the
    # same field often appears in near-duplicate Get/non-Get schema pairs per
    # spec, and a tight limit crowds out the spec file the query is about
    # (e.g. limit=4 returned only ap-uplink/mesh "opmode", never wlan's).
    for term in flat:
        if len(term) < 4:
            continue
        for row in get_enum(term, limit=16, db_path=db_path):
            blob = (f"{row['spec_file']} {row['schema_name']} {row['path']} "
                    f"{row.get('description') or ''} {' '.join(map(str, row.get('enums') or []))} "
                    f"{row.get('enum_descriptions') or ''}")
            add("enum", row["spec_file"], f"{row['schema_name']}.{row['path']}",
                _fmt_enum_field(row), matched(blob), exact=True)

    # 2. Exact endpoint lookups for hyphenated tokens (one progressive trim)
    for term in (g[0] for g in groups if "-" in g[0]):
        for candidate in (term, term.rsplit("-", 1)[0]):
            if "-" not in candidate:  # trimmed to a single bare word — too generic
                continue
            rows = get_endpoint(candidate, limit=6, db_path=db_path)
            if rows:
                for row in rows:
                    blob = (f"{row['spec_file']} {row['method']} {row['path']} "
                            f"{row.get('summary') or ''} {row.get('description') or ''}")
                    add("endpoint", row["spec_file"], f"{row['method']} {row['path']}",
                        _fmt_endpoint(row), matched(blob), exact=True)
                break

    # 3. FTS prefix search, re-ranked by distinct-concept coverage
    conn = connect(db_path)
    try:
        # Deep candidate fetch: bm25 penalizes long bodies, so with a shallow
        # cap the big multi-term schema rows (the ones that actually clear the
        # coverage threshold) get starved by hundreds of short single-term rows.
        match_expr = " OR ".join(f'"{s}"*' for s in flat)
        rows = conn.execute(
            "SELECT kind, spec_file, ref, body FROM fts WHERE fts MATCH ? "
            "ORDER BY bm25(fts) LIMIT 400",
            (match_expr,),
        ).fetchall()
        for r in rows:
            score = matched(f"{r['spec_file']} {r['body']}")
            if score < threshold:
                continue
            if r["kind"] == "endpoint":
                method, _, path = r["ref"].partition(" ")
                ep = get_endpoint(path, method=method, limit=1, db_path=db_path)
                text = _fmt_endpoint(ep[0]) if ep else r["ref"]
            else:
                # Schema hit: surface only the fields the query actually asked about
                fields = conn.execute(
                    "SELECT field_name, path, type, description, enums FROM fields "
                    "WHERE schema_name = ? AND spec_file = ? LIMIT 400",
                    (r["ref"], r["spec_file"]),
                ).fetchall()
                parts = []
                for f in fields:
                    enums = json.loads(f["enums"]) if f["enums"] else []
                    fblob = f"{f['field_name']} {f['description'] or ''} {' '.join(map(str, enums))}"
                    if matched(fblob):
                        enum_sfx = f" enum: {', '.join(map(str, enums[:24]))}" if enums else ""
                        parts.append(f"{f['path']} ({f['type']}){enum_sfx}")
                    if len(parts) >= 8:
                        break
                text = f"Schema {r['ref']} [{r['spec_file']}]: " + "; ".join(parts)
            add(r["kind"], r["spec_file"], r["ref"], text, score, exact=False)
    finally:
        conn.close()

    # Exact hits first, then by concept coverage. Non-exact hits must clear the
    # full threshold; exact hits are trusted but still need one corroborating
    # concept beyond the name that matched them (a lone field-name collision on
    # a multi-term query is noise, e.g. MVRP "registration" for "MAC registration").
    exact_floor = min(2, n_terms)
    out = [h for h in hits.values()
           if (h["_exact"] and h["score"] >= exact_floor) or h["score"] >= threshold]
    out.sort(key=lambda h: (not h["_exact"], -h["score"]))
    for h in out:
        h.pop("_exact")
    return out[:top_k]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    build_group = ap.add_mutually_exclusive_group()
    build_group.add_argument("--build", action="store_true")
    build_group.add_argument("--rebuild-shared", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--enum")
    ap.add_argument("--lookup", help="natural-language lookup (as the MCP tool runs it)")
    args = ap.parse_args()
    if args.build:
        print(json.dumps(build(), indent=2))
    if args.rebuild_shared:
        print(json.dumps(rebuild_shared(), indent=2))
    if args.query:
        print(json.dumps(search(args.query), indent=2))
    if args.enum:
        print(json.dumps(get_enum(args.enum), indent=2))
    if args.lookup:
        print(json.dumps(lookup(args.lookup), indent=2))
