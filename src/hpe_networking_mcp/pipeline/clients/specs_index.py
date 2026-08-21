"""SQLite structured index over exact OpenAPI specs — exact API lookup.

Vector search is the wrong tool for "what enum values does field X accept" or
"which endpoint configures Y": those need lossless, authoritative answers.
This module parses ingestion/sources/openapi_specs/*.json into SQLite with
FTS5 keyword search, giving exact endpoint / schema / field / enum lookup,
plus a per-platform ``responses`` table backing reactive error hints (see
``hpe_networking_mcp.pipeline.clients.error_help``) — what a status code
documents across a platform's API, for a failed tool call.
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
from urllib.parse import urlparse

from hpe_networking_mcp._paths import repo_root

# See lance_client.ROOT: ``parents[2]`` is the package dir under src/ layout,
# so the structured spec index was looked up at a path that never exists.
ROOT = repo_root()
SPECS_DIR = ROOT / "ingestion" / "sources" / "openapi_specs"
PRODUCT_SPECS_DIR = ROOT / "ingestion" / "sources" / "product_specs"
DB_PATH = ROOT / "data" / "specs.sqlite"
OPENAPI_MANIFEST_PATH = ROOT / "ingestion" / "openapi_registry_manifest.json"
PRODUCT_SPECS_MANIFEST_PATH = ROOT / "ingestion" / "product_specs_manifest.json"
# The committed offline corpus. ``ingestion/sources/`` is git-ignored scrape
# output, so a fresh clone has no OpenAPI documents to index at all; this
# directory ships them. See ``default_source_dirs``.
VENDOR_OPENAPI_DIR = ROOT / "vendor" / "openapi"
_FULL_REBUILD_COMMAND = (
    "uv run python -m hpe_networking_mcp.pipeline.clients.specs_index --rebuild-shared"
)
_DEFAULT_SOURCE_DIRS: dict[str, Path] = {
    "openapi_specs": SPECS_DIR,
    "product_specs": PRODUCT_SPECS_DIR,
}
_DEFAULT_MANIFEST_PATHS: dict[str, Path] = {
    "openapi_specs": OPENAPI_MANIFEST_PATH,
    "product_specs": PRODUCT_SPECS_MANIFEST_PATH,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY,
    source_family TEXT, source_url TEXT, platform TEXT, version TEXT, spec_version TEXT,
    identity TEXT, spec_name TEXT, spec_file TEXT, server TEXT,
    method TEXT, path TEXT, operation_id TEXT, summary TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS schemas (
    id INTEGER PRIMARY KEY,
    source_family TEXT, source_url TEXT, platform TEXT, version TEXT, spec_version TEXT,
    identity TEXT, spec_name TEXT, spec_file TEXT, name TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY,
    source_family TEXT, source_url TEXT, platform TEXT, version TEXT, spec_version TEXT,
    schema_identity TEXT, spec_name TEXT, spec_file TEXT, schema_name TEXT,
    field_name TEXT, path TEXT, type TEXT, description TEXT,
    enums TEXT, enum_descriptions TEXT
);
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY,
    source_family TEXT, source_url TEXT, platform TEXT, version TEXT, spec_version TEXT,
    spec_file TEXT, method TEXT, path TEXT,
    status_code TEXT, description TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    kind, spec_file, ref, body,
    source_family UNINDEXED, source_url UNINDEXED, platform UNINDEXED,
    version UNINDEXED, spec_version UNINDEXED, identity UNINDEXED
);
CREATE INDEX IF NOT EXISTS idx_fields_name ON fields(field_name);
CREATE INDEX IF NOT EXISTS idx_fields_schema ON fields(schema_name);
CREATE INDEX IF NOT EXISTS idx_fields_identity ON fields(schema_identity);
CREATE INDEX IF NOT EXISTS idx_fields_source_platform_version
    ON fields(source_family, platform, version, spec_version);
CREATE INDEX IF NOT EXISTS idx_endpoints_path ON endpoints(path);
CREATE INDEX IF NOT EXISTS idx_endpoints_identity ON endpoints(identity);
CREATE INDEX IF NOT EXISTS idx_endpoints_operation_id
    ON endpoints(operation_id COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_endpoints_source_platform_version
    ON endpoints(source_family, platform, version, spec_version);
CREATE INDEX IF NOT EXISTS idx_schemas_identity ON schemas(identity);
CREATE INDEX IF NOT EXISTS idx_schemas_source_platform_version
    ON schemas(source_family, platform, version, spec_version);
CREATE INDEX IF NOT EXISTS idx_responses_platform_code
    ON responses(platform, status_code);
"""

# Substring hints in an OpenAPI ``servers[0].url`` that reliably identify
# which product platform a spec belongs to. Data-driven from the specs
# actually ingested today (Central + Mist) — extend this tuple, not the
# lookup logic, when a new product's OpenAPI spec is added to
# ingestion/sources/openapi_specs/. Order matters only if a URL could match
# more than one entry; none currently do.
_PLATFORM_SERVER_HINTS: tuple[tuple[str, str], ...] = (
    ("arubanetworks.com", "central"),
    ("mist.com", "mist"),
)
_PROJECT_PLATFORM_HINTS: dict[str, str] = {
    "aruba-new-central": "central",
    "aruba-new-central-config": "central",
    "aruba-central": "central",
    "aruba-aos": "aos8",
    "aruba-aoscx": "aoscx",
    "aruba-cppm": "clearpass",
    "aruba-uxi": "uxi",
    "aruba-edgeconnect": "edgeconnect",
    "aruba-fabric-composer": "afc",
}
_SECTION_PLATFORM_HINTS: dict[str, str] = {
    "central": "central",
    "aos8": "aos8",
    "aoscx": "aoscx",
    "cppm": "clearpass",
    "uxi": "uxi",
    "edgeconnect": "edgeconnect",
    "afc": "afc",
}
_SPEC_FILE_PLATFORM_HINTS: tuple[tuple[str, str], ...] = (
    ("mist.openapi", "mist"),
    ("mist-openapi", "mist"),
)


def _platform_for_server(server: str | None) -> str | None:
    """Best-effort platform key for an OpenAPI spec's ``servers[0].url``.

    Returns ``None`` (never a guess) for shared/ambiguous hosts — e.g.
    ``sso.common.cloud.hpe.com`` is a shared HPE SSO/authorization endpoint
    reused across specs, not a single product's API surface.
    """
    low = (server or "").lower()
    for needle, platform in _PLATFORM_SERVER_HINTS:
        if needle in low:
            return platform
    return None


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_family_for_dir(specs_dir: Path) -> str:
    name = specs_dir.name.strip().lower()
    return name if name in _DEFAULT_SOURCE_DIRS else "openapi_specs"


def _load_manifest_metadata(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _openapi_manifest_metadata(path: Path) -> dict[str, dict[str, Any]]:
    data = _load_manifest_metadata(path)
    registries = data.get("registries") if isinstance(data, dict) else None
    if not isinstance(registries, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in registries.values():
        if not isinstance(entry, dict):
            continue
        spec_file = Path(str(entry.get("output_path") or "")).name
        if not spec_file:
            continue
        out[spec_file] = {
            "project": _clean_text(entry.get("project")),
            "version": _clean_text(entry.get("portal_version")),
            "spec_version": _clean_text(entry.get("spec_version")),
            "source_url": _clean_text(entry.get("source_url")),
        }
    return out


def _product_manifest_metadata(path: Path) -> dict[str, dict[str, Any]]:
    data = _load_manifest_metadata(path)
    specs = data.get("specs") if isinstance(data, dict) else None
    if not isinstance(specs, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in specs:
        if not isinstance(entry, dict):
            continue
        spec_file = Path(str(entry.get("output_path") or "")).name
        if not spec_file:
            continue
        out[spec_file] = {
            "project": _clean_text(entry.get("project")),
            "section": _clean_text(entry.get("section")),
            "version": _clean_text(entry.get("branch")),
            "source_url": _clean_text(entry.get("source_url")),
        }
    return out


def _source_metadata_maps(
    manifest_paths: dict[str, Path] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    manifest_paths = {**_DEFAULT_MANIFEST_PATHS, **(manifest_paths or {})}
    return {
        "openapi_specs": _openapi_manifest_metadata(manifest_paths["openapi_specs"]),
        "product_specs": _product_manifest_metadata(manifest_paths["product_specs"]),
    }


def _platform_for_spec(
    spec_file: str,
    server: str | None,
    source_meta: dict[str, Any],
) -> str | None:
    if platform := _platform_for_server(server):
        return platform
    project = str(source_meta.get("project") or "").lower()
    if project in _PROJECT_PLATFORM_HINTS:
        return _PROJECT_PLATFORM_HINTS[project]
    section = str(source_meta.get("section") or "").lower()
    if section in _SECTION_PLATFORM_HINTS:
        return _SECTION_PLATFORM_HINTS[section]
    low_file = spec_file.lower()
    for needle, platform in _SPEC_FILE_PLATFORM_HINTS:
        if needle in low_file:
            return platform
    return None


def _version_metadata(
    spec: dict[str, Any],
    source_meta: dict[str, Any],
) -> tuple[str | None, str | None]:
    info = spec.get("info")
    info = info if isinstance(info, dict) else {}
    spec_version = _clean_text(info.get("version"))
    version = _clean_text(source_meta.get("version")) or spec_version
    return version, spec_version


def _normalized_server(server: str | None) -> str:
    raw = str(server or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        return f"{parsed.netloc}{path}"
    return raw.rstrip("/")


def _endpoint_identity(
    *,
    source_family: str,
    platform: str | None,
    server: str | None,
    version: str | None,
    spec_version: str | None,
    method: str,
    path: str,
) -> str:
    scope = platform or _normalized_server(server) or source_family
    version_key = version or spec_version or ""
    return "|".join(
        (
            source_family,
            scope,
            version_key.lower(),
            method.upper(),
            path,
        )
    )


def _schema_identity(
    *,
    source_family: str,
    platform: str | None,
    server: str | None,
    version: str | None,
    spec_version: str | None,
    schema_name: str,
    field_rows: list[tuple[str, str, str | None, str | None]],
) -> str:
    scope = platform or _normalized_server(server) or source_family
    version_key = version or spec_version or ""
    signature = json.dumps(sorted(field_rows), separators=(",", ":"), sort_keys=False)
    return "|".join(
        (
            source_family,
            scope,
            version_key.lower(),
            schema_name,
            signature,
        )
    )


def _holds_specs(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.json"))


def default_source_dirs() -> dict[str, Path]:
    """The directories a default build reads, vendored corpus included.

    ``ingestion/sources/openapi_specs`` is git-ignored scrape output. Absent
    it there is nothing to index and ``lookup_api`` answers nothing on a
    fresh clone, so the committed ``vendor/openapi`` corpus stands in. A
    directory holding specs always wins, so a developer who has just
    refreshed the scrape keeps reading their own output.

    Both directories map to the ``openapi_specs`` source family --
    ``_source_family_for_dir`` falls back to it for any directory not named
    after a family, and ``openapi`` is not one -- so rows, identities and the
    ``idx_*_source_platform_version`` indexes are identical either way.
    """
    dirs = dict(_DEFAULT_SOURCE_DIRS)
    if not _holds_specs(dirs["openapi_specs"]) and _holds_specs(VENDOR_OPENAPI_DIR):
        dirs["openapi_specs"] = VENDOR_OPENAPI_DIR
    return dirs


def _coerce_source_dirs(
    specs_dir: Path | None,
    source_dirs: dict[str, Path] | None,
) -> dict[str, Path]:
    if source_dirs is not None:
        return dict(source_dirs)
    if specs_dir is not None:
        return {_source_family_for_dir(specs_dir): specs_dir}
    return default_source_dirs()


def connect(db_path: Path = DB_PATH, *, create: bool = False) -> sqlite3.Connection:
    """Open the structured index.

    Args:
        db_path: The index to open; defaults to ``data/specs.sqlite``.
        create: Open read-write, creating the file when absent. Only the
            builder wants this.

    Query paths must never create. ``sqlite3.connect`` on a plain path opens
    read-write and materializes an empty database when the file is missing,
    so one best-effort read in a corpus-free checkout (``reactive_hint`` on a
    failed dispatch, say) leaves a zero-byte ``data/specs.sqlite`` behind.
    Everything that probes for an index with ``is_file()`` then believes one
    exists, and derived-fact tooling fails on the absent tables instead of
    taking its documented no-data path. Read-only against a missing file
    raises ``sqlite3.OperationalError``, which every reader here already
    degrades on.
    """
    if create:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


def _response_description(spec: dict, resp: dict) -> str:
    """A response object's description, resolving a local ``$ref`` first.

    OpenAPI's standard "reusable responses" pattern
    (https://spec.openapis.org/oas/v3.1.0#components-object) defines shared
    4xx/5xx shapes once under ``components.responses`` and ``$ref``s them
    from every operation that returns the same error — exactly the codes
    (401/403/429) this feature most needs a description for. A response
    entry with a ``$ref`` has no inline ``description`` of its own, so
    reading ``resp["description"]`` directly silently drops most responses
    in specs that use this idiom.
    """
    ref = resp.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/responses/"):
        name = ref.rsplit("/", 1)[-1]
        resolved = spec.get("components", {}).get("responses", {}).get(name)
        if isinstance(resolved, dict):
            return resolved.get("description", "") or ""
        return ""
    return resp.get("description", "") or ""


def _populate_openapi_tables(
    conn: sqlite3.Connection,
    source_dirs: dict[str, Path],
    manifest_paths: dict[str, Path] | None = None,
) -> dict[str, int]:
    """Populate OpenAPI-owned tables in an initialized SQLite connection."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS endpoints;
        DROP TABLE IF EXISTS schemas;
        DROP TABLE IF EXISTS fields;
        DROP TABLE IF EXISTS responses;
        DROP TABLE IF EXISTS fts;
        """
    )
    conn.executescript(_SCHEMA)

    counts = {
        "specs": 0,
        "endpoints": 0,
        "schemas": 0,
        "fields": 0,
        "responses": 0,
        "skipped": 0,
    }
    metadata_by_source = _source_metadata_maps(manifest_paths)
    for source_family, specs_dir in source_dirs.items():
        source_meta_map = metadata_by_source.get(source_family, {})
        for path in sorted(specs_dir.glob("*.json")):
            try:
                spec = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                counts["skipped"] += 1
                continue
            if not isinstance(spec, dict):
                counts["skipped"] += 1
                continue

            source_meta = source_meta_map.get(path.name, {})
            info = spec.get("info")
            info = info if isinstance(info, dict) else {}
            spec_name = info.get("title", path.stem)
            spec_file = path.name
            servers = spec.get("servers") or []
            first_server = servers[0] if servers else {}
            server = first_server.get("url", "") if isinstance(first_server, dict) else ""
            platform = _platform_for_spec(spec_file, server, source_meta)
            version, spec_version = _version_metadata(spec, source_meta)
            source_url = _clean_text(source_meta.get("source_url"))
            exact_records = 0

            for api_path, item in (spec.get("paths") or {}).items():
                if not isinstance(item, dict):
                    continue
                for method, op in item.items():
                    if method not in ("get", "post", "put", "patch", "delete"):
                        continue
                    if not isinstance(op, dict):
                        continue
                    summary = op.get("summary", "")
                    desc = op.get("description", "")
                    identity = _endpoint_identity(
                        source_family=source_family,
                        platform=platform,
                        server=server,
                        version=version,
                        spec_version=spec_version,
                        method=method,
                        path=api_path,
                    )
                    conn.execute(
                        "INSERT INTO endpoints "
                        "(source_family, source_url, platform, version, spec_version, identity, "
                        "spec_name, spec_file, server, method, path, operation_id, "
                        "summary, description) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            source_family,
                            source_url,
                            platform,
                            version,
                            spec_version,
                            identity,
                            spec_name,
                            spec_file,
                            server,
                            method.upper(),
                            api_path,
                            op.get("operationId", ""),
                            summary,
                            desc,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO fts "
                        "(kind, spec_file, ref, body, source_family, source_url, platform, "
                        "version, spec_version, identity) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            "endpoint",
                            spec_file,
                            f"{method.upper()} {api_path}",
                            f"{spec_name} {api_path} {summary} {desc}",
                            source_family,
                            source_url,
                            platform,
                            version,
                            spec_version,
                            identity,
                        ),
                    )
                    counts["endpoints"] += 1
                    exact_records += 1
                    # Status-code -> documented meaning, grouped by platform so a
                    # failed tool call can be told what e.g. 429/403 mean on the
                    # platform it hit, without matching the specific endpoint.
                    if platform:
                        for status_code, resp in (op.get("responses") or {}).items():
                            if not isinstance(resp, dict) or not str(status_code).isdigit():
                                continue
                            resp_desc = _response_description(spec, resp)
                            if not resp_desc:
                                continue
                            conn.execute(
                                "INSERT INTO responses "
                                "(source_family, source_url, platform, version, spec_version, "
                                "spec_file, method, path, status_code, description) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (
                                    source_family,
                                    source_url,
                                    platform,
                                    version,
                                    spec_version,
                                    spec_file,
                                    method.upper(),
                                    api_path,
                                    str(status_code),
                                    resp_desc,
                                ),
                            )
                            counts["responses"] += 1

            for schema_name, schema in (spec.get("components", {}).get("schemas") or {}).items():
                if not isinstance(schema, dict):
                    continue
                s_desc = schema.get("description", "")
                field_rows: list[tuple[str, str, str | None, str | None]] = []
                prop_rows: list[tuple[str, str, str, str, str | None, str | None]] = []
                prop_texts = []
                for fpath, field, fdef in _walk_fields(schema, ""):
                    enums = fdef.get("enum")
                    enum_desc = fdef.get("x-enumDescriptions")
                    enum_json = json.dumps(enums) if enums else None
                    enum_desc_json = json.dumps(enum_desc) if enum_desc else None
                    field_type = str(fdef.get("type", ""))
                    field_desc = fdef.get("description", "")
                    field_rows.append((fpath, field_type, enum_json, enum_desc_json))
                    prop_rows.append(
                        (field, fpath, field_type, field_desc, enum_json, enum_desc_json)
                    )
                    if enums or field_desc:
                        prop_texts.append(
                            f"{field} {field_desc} {' '.join(map(str, enums or []))}"
                        )
                identity = _schema_identity(
                    source_family=source_family,
                    platform=platform,
                    server=server,
                    version=version,
                    spec_version=spec_version,
                    schema_name=schema_name,
                    field_rows=field_rows,
                )
                conn.execute(
                    "INSERT INTO schemas "
                    "(source_family, source_url, platform, version, spec_version, identity, "
                    "spec_name, spec_file, name, description) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_family,
                        source_url,
                        platform,
                        version,
                        spec_version,
                        identity,
                        spec_name,
                        spec_file,
                        schema_name,
                        s_desc,
                    ),
                )
                counts["schemas"] += 1
                exact_records += 1
                for field, fpath, field_type, field_desc, enum_json, enum_desc_json in prop_rows:
                    conn.execute(
                        "INSERT INTO fields "
                        "(source_family, source_url, platform, version, spec_version, "
                        "schema_identity, spec_name, spec_file, schema_name, field_name, "
                        "path, type, description, enums, enum_descriptions) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            source_family,
                            source_url,
                            platform,
                            version,
                            spec_version,
                            identity,
                            spec_name,
                            spec_file,
                            schema_name,
                            field,
                            fpath,
                            field_type,
                            field_desc,
                            enum_json,
                            enum_desc_json,
                        ),
                    )
                    counts["fields"] += 1
                conn.execute(
                    "INSERT INTO fts "
                    "(kind, spec_file, ref, body, source_family, source_url, platform, "
                    "version, spec_version, identity) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "schema",
                        spec_file,
                        schema_name,
                        f"{spec_name} {schema_name} {s_desc} {' '.join(prop_texts)}",
                        source_family,
                        source_url,
                        platform,
                        version,
                        spec_version,
                        identity,
                    ),
                )
            if exact_records > 0:
                counts["specs"] += 1
            else:
                counts["skipped"] += 1
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
    specs_dir: Path | None = None,
    db_path: Path = DB_PATH,
    *,
    source_dirs: dict[str, Path] | None = None,
    manifest_paths: dict[str, Path] | None = None,
    preserve_shared: bool = True,
) -> dict[str, int]:
    """Parse all OpenAPI specs into the shared SQLite index.

    Builds into a sibling temp file and atomically replaces the live path only
    after a full commit. Existing non-OpenAPI tables are copied forward by
    default because advisory and lifecycle indexes share this artifact. Full
    ingestion and ``--rebuild-shared`` set ``preserve_shared=False`` because
    they rebuild every shared table.
    """
    source_dirs = _coerce_source_dirs(specs_dir, source_dirs)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    conn = connect(tmp_path, create=True)
    try:
        if preserve_shared and db_path.exists():
            source = connect(db_path)
            try:
                _check_integrity(source, db_path)
                source.backup(conn)
            finally:
                source.close()
        counts = _populate_openapi_tables(
            conn,
            source_dirs,
            manifest_paths=manifest_paths,
        )
        if counts["specs"] <= 0 or counts["endpoints"] <= 0:
            raise RuntimeError(
                "no OpenAPI records found under "
                f"{', '.join(str(path) for path in source_dirs.values())}; "
                "refresh the git-ignored ingestion sources before rebuilding"
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
    from hpe_networking_mcp.pipeline.clients import advisory_index, aoscx_release_index

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
        feature_history = list(
            (knowledge_sources / "feature_navigator").glob("cx-*-history.json")
        )
        feature_counts = (
            aoscx_release_index.build_feature_index(
                sources_dir=knowledge_sources,
                db_path=staging_path,
            )
            if feature_history
            else None
        )
        if (
            knowledge_counts.get("advisories", 0) <= 0
            or knowledge_counts.get("lifecycle_events", 0) <= 0
        ):
            raise RuntimeError(
                "no advisory or lifecycle records were indexed; refresh the "
                "git-ignored ingestion sources before rebuilding"
            )
        result: dict[str, dict[str, int]] = {
            "openapi": openapi_counts,
            "knowledge": knowledge_counts,
        }
        if feature_counts is not None:
            result["aoscx_features"] = feature_counts
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


def _metadata_filters(
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    alias: str = "",
) -> tuple[list[str], list[Any]]:
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    if source:
        clauses.append(f"LOWER({prefix}source_family) = LOWER(?)")
        params.append(source)
    if platform:
        clauses.append(f"LOWER({prefix}platform) = LOWER(?)")
        params.append(platform)
    if version:
        clauses.append(
            "("
            f"LOWER(COALESCE({prefix}version, '')) = LOWER(?) "
            f"OR LOWER(COALESCE({prefix}spec_version, '')) = LOWER(?)"
            ")"
        )
        params.extend([version, version])
    return clauses, params


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if row.get("platform"):
        out["platform"] = row["platform"]
    if row.get("version"):
        out["version"] = row["version"]
    api_version = row.get("spec_version")
    if api_version and api_version != row.get("version"):
        out["api_version"] = api_version
    if row.get("source_url"):
        out["source_url"] = row["source_url"]
    return out


def _externalize_row(
    row: sqlite3.Row | dict[str, Any],
    *,
    include_metadata: bool = False,
) -> dict[str, Any]:
    data = dict(row)
    data.pop("identity", None)
    data.pop("schema_identity", None)
    if not include_metadata:
        data.pop("source_family", None)
        data.pop("source_url", None)
        data.pop("platform", None)
        data.pop("version", None)
        data.pop("spec_version", None)
    return data


def _dedupe_rows(
    rows: list[sqlite3.Row],
    *,
    key: str,
    limit: int,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        row_dict = dict(row)
        identity = str(
            row_dict.get(key)
            or f"{row_dict.get('spec_file')}#"
            f"{row_dict.get('ref') or row_dict.get('path') or row_dict.get('name')}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        out.append(_externalize_row(row_dict, include_metadata=include_metadata))
        if len(out) >= limit:
            break
    return out


def search(
    query: str,
    kind: str | None = None,
    limit: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """FTS keyword search across endpoints + schemas. kind: endpoint|schema."""
    conn = connect(db_path)
    sql = (
        "SELECT kind, spec_file, ref, snippet(fts, 3, '', '', '…', 24) AS snippet, "
        "source_family, source_url, platform, version, spec_version, identity "
        "FROM fts WHERE fts MATCH ?"
    )
    params: list[Any] = [_fts_escape(query)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    clauses, clause_params = _metadata_filters(
        source=source,
        platform=platform,
        version=version,
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(clause_params)
    sql += " LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _dedupe_rows(rows, key="identity", limit=limit, include_metadata=include_metadata)


def get_endpoint(
    path_contains: str,
    method: str | None = None,
    limit: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Exact-ish endpoint lookup by path substring (and optional method)."""
    conn = connect(db_path)
    sql = (
        "SELECT source_family, source_url, platform, version, spec_version, identity, "
        "spec_name, spec_file, server, method, path, summary, description "
        "FROM endpoints WHERE path LIKE ?"
    )
    params: list[Any] = [f"%{path_contains}%"]
    if method:
        sql += " AND method = ?"
        params.append(method.upper())
    clauses, clause_params = _metadata_filters(
        source=source,
        platform=platform,
        version=version,
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(clause_params)
    sql += " ORDER BY spec_file, id LIMIT ?"
    params.append(max(limit * 8, limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _dedupe_rows(rows, key="identity", limit=limit, include_metadata=include_metadata)


def get_exact_endpoint(
    method: str,
    path: str,
    limit: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Exact endpoint lookup by HTTP method and literal OpenAPI path."""
    conn = connect(db_path)
    sql = (
        "SELECT source_family, source_url, platform, version, spec_version, identity, "
        "spec_name, spec_file, server, method, path, summary, description "
        "FROM endpoints WHERE method = ? AND path = ?"
    )
    params: list[Any] = [method.upper(), path]
    clauses, clause_params = _metadata_filters(
        source=source,
        platform=platform,
        version=version,
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(clause_params)
    sql += " ORDER BY spec_file, id LIMIT ?"
    params.append(max(limit * 8, limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _dedupe_rows(rows, key="identity", limit=limit, include_metadata=include_metadata)


def get_endpoint_by_operation_id(
    operation_id: str,
    limit: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Exact case-insensitive endpoint lookup by OpenAPI operationId."""
    conn = connect(db_path)
    sql = (
        "SELECT source_family, source_url, platform, version, spec_version, identity, "
        "spec_name, spec_file, server, method, path, summary, description "
        "FROM endpoints WHERE operation_id = ? COLLATE NOCASE"
    )
    params: list[Any] = [operation_id]
    clauses, clause_params = _metadata_filters(
        source=source,
        platform=platform,
        version=version,
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(clause_params)
    sql += " ORDER BY spec_file, id LIMIT ?"
    params.append(max(limit * 8, limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _dedupe_rows(rows, key="identity", limit=limit, include_metadata=include_metadata)


def get_schema(
    name_contains: str,
    limit: int = 5,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Schema lookup with its full field list (types, enums)."""
    conn = connect(db_path)
    try:
        sql = (
            "SELECT source_family, source_url, platform, version, spec_version, identity, "
            "spec_name, spec_file, name, description FROM schemas WHERE name LIKE ?"
        )
        params: list[Any] = [f"%{name_contains}%"]
        clauses, clause_params = _metadata_filters(
            source=source,
            platform=platform,
            version=version,
        )
        if clauses:
            sql += " AND " + " AND ".join(clauses)
            params.extend(clause_params)
        sql += " ORDER BY spec_file, id LIMIT ?"
        params.append(max(limit * 8, limit))
        schemas = conn.execute(sql, params).fetchall()
        out = []
        seen: set[str] = set()
        for s in schemas:
            identity = str(s["identity"] or f"{s['spec_file']}#{s['name']}")
            if identity in seen:
                continue
            seen.add(identity)
            fields = conn.execute(
                "SELECT source_family, source_url, platform, version, spec_version, "
                "schema_identity, field_name, path, type, description, enums, "
                "enum_descriptions FROM fields WHERE schema_identity = ?",
                (s["identity"],),
            ).fetchall()
            schema_row = _externalize_row(s, include_metadata=include_metadata)
            schema_row["fields"] = [
                {
                    **_externalize_row(f, include_metadata=include_metadata),
                    "enums": json.loads(f["enums"]) if f["enums"] else None,
                    "enum_descriptions": (
                        json.loads(f["enum_descriptions"])
                        if f["enum_descriptions"]
                        else None
                    ),
                }
                for f in fields
            ]
            out.append(schema_row)
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out


def get_enum(
    field_name: str,
    schema_contains: str | None = None,
    limit: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Authoritative enum values for a field, across all specs."""
    conn = connect(db_path)
    sql = (
        "SELECT source_family, source_url, platform, version, spec_version, "
        "schema_identity, spec_name, spec_file, schema_name, field_name, path, "
        "type, description, enums, enum_descriptions "
        "FROM fields WHERE field_name = ? AND enums IS NOT NULL"
    )
    params: list[Any] = [field_name]
    if schema_contains:
        sql += " AND schema_name LIKE ?"
        params.append(f"%{schema_contains}%")
    clauses, clause_params = _metadata_filters(
        source=source,
        platform=platform,
        version=version,
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(clause_params)
    sql += " ORDER BY spec_file, id LIMIT ?"
    params.append(max(limit * 8, limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for r in rows:
        dedupe_key = (
            str(r["schema_identity"] or ""),
            str(r["path"]),
            json.dumps(json.loads(r["enums"]), separators=(",", ":")),
            r["enum_descriptions"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(
            {
                **_externalize_row(r, include_metadata=include_metadata),
                "enums": json.loads(r["enums"]),
                "enum_descriptions": (
                    json.loads(r["enum_descriptions"])
                    if r["enum_descriptions"]
                    else None
                ),
            }
        )
        if len(out) >= limit:
            break
    return out


def get_response_description(
    platform: str,
    status_code: int | str,
    min_share: float = 0.6,
    db_path: Path = DB_PATH,
) -> str | None:
    """The API's own documented meaning of a status code for a platform.

    Returns the most-common response ``description`` for ``(platform,
    status_code)`` — but only when it dominates (``>= min_share`` of that
    code's *distinct* documented occurrences) — so a status code with one
    consistent meaning across the API (429/401/403 on most platforms)
    enriches safely, while one with genuinely different per-endpoint
    meanings returns ``None`` (a misleading single sentence) rather than an
    unreliable guess.

    Deduplicates identical ``(method, path, status_code, description)``
    rows before voting: Aruba's downloaded spec corpus ships the same
    operation in more than one overlapping spec file (e.g. a grouped
    "config" bundle and a narrower per-feature bundle both defining
    ``DELETE /network-config/v1alpha1/cnac-dpp-reg/{id}``), so counting raw
    rows would let a single documented endpoint outvote several genuinely
    distinct ones just because its spec happens to be mirrored more times.

    Never raises: a missing db file, a pre-rebuild index without the
    ``responses`` table, or a corrupt file all degrade to ``None`` — this is
    best-effort enrichment, not a query users depend on directly.
    """
    if not platform:
        return None
    try:
        conn = connect(db_path)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT description, COUNT(*) AS c FROM ("
            "  SELECT DISTINCT method, path, status_code, description"
            "  FROM responses"
            "  WHERE platform = ? AND status_code = ? "
            "        AND description IS NOT NULL AND description != ''"
            ") GROUP BY description ORDER BY c DESC",
            (platform, str(status_code)),
        ).fetchall()
    except sqlite3.Error:
        # Most commonly "no such table: responses" -- a real db built before
        # this feature existed. Best-effort enrichment degrades to nothing.
        return None
    finally:
        conn.close()
    if not rows:
        return None
    total = sum(r["c"] for r in rows)
    top = rows[0]
    if total and (top["c"] / total) >= min_share:
        return top["description"]
    return None


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


def _hit_from_row(
    row: dict[str, Any],
    *,
    kind: str,
    ref: str,
    text: str,
    score: int,
    include_metadata: bool = False,
) -> dict[str, Any]:
    hit = {
        "text": text,
        "source": row.get("source_family") or "openapi_specs",
        "file_path": f"{row.get('source_family') or 'openapi_specs'}/{row['spec_file']}#{ref}",
        "kind": kind,
        "score": score,
    }
    if include_metadata:
        hit.update(_row_metadata(row))
    return hit


def _exact_endpoint_hits(
    rows: list[dict[str, Any]],
    *,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    return [
        _hit_from_row(
            row,
            kind="endpoint",
            ref=f"{row['method']} {row['path']}",
            text=_fmt_endpoint(row),
            score=100,
            include_metadata=include_metadata,
        )
        for row in rows
    ]


def _fmt_enum_field(row: dict[str, Any]) -> str:
    enums = row.get("enums") or []
    text = (f"{row['schema_name']}.{row['path']} ({row['spec_name']}) "
            f"type={row.get('type', '')}: {(row.get('description') or '')[:200]} "
            f"Enum: {', '.join(map(str, enums[:24]))}")
    return text.strip()


def lookup(
    query: str,
    top_k: int = 10,
    db_path: Path = DB_PATH,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Exact API lookup for a natural-language question. Returns [] when the
    specs have no confident answer (caller should fall back to prose search).

    Four strategies:
      1. exact HTTP method/path or operationId equality lookup
      2. exact enum/field match for field-like terms (get_enum)
      3. exact endpoint match for hyphenated path-like tokens, with one
         progressive right-trim (device-firmware-upgrade -> device-firmware)
      4. FTS prefix search re-ranked by how many distinct query terms the row
         actually contains (bm25 alone ranks short generic rows too high)

    Hits are {"text", "source", "file_path", "kind", "score"} by default.
    ``include_metadata=True`` adds exact spec-grounded platform/version
    provenance for callers that need it without breaking older consumers.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"specs index missing at {db_path} — build it with "
            "`python -m hpe_networking_mcp.pipeline.clients.specs_index --build`"
        )
    top_k = max(1, min(20, top_k))
    try:
        return _cached_lookup(
            query,
            top_k,
            db_path,
            source=source,
            platform=platform,
            version=version,
            include_metadata=include_metadata,
        )
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


def _cached_lookup(
    query: str,
    top_k: int,
    db_path: Path,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    # The filters and the metadata flag change the result set, so they belong
    # in the cache key. Keying on query/top_k alone would let a filtered
    # lookup serve an unfiltered neighbour's hits.
    key = (
        str(db_path),
        query.strip(),
        top_k,
        source,
        platform,
        version,
        include_metadata,
        _db_fingerprint(db_path),
    )
    cached = _lookup_cache.get(key)
    if cached is not None:
        _lookup_cache.move_to_end(key)
        return [dict(hit) for hit in cached]
    hits = _lookup(
        query,
        top_k,
        db_path,
        source=source,
        platform=platform,
        version=version,
        include_metadata=include_metadata,
    )
    _lookup_cache[key] = [dict(hit) for hit in hits]
    _lookup_cache.move_to_end(key)
    while len(_lookup_cache) > _LOOKUP_CACHE_MAX:
        _lookup_cache.popitem(last=False)
    return [dict(hit) for hit in hits]


def _lookup(
    query: str,
    top_k: int,
    db_path: Path,
    *,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    stripped = query.strip()
    exact_endpoint = _EXACT_ENDPOINT_RE.fullmatch(stripped)
    if exact_endpoint:
        method, path = exact_endpoint.groups()
        return _exact_endpoint_hits(
            get_exact_endpoint(
                method,
                path,
                limit=top_k,
                db_path=db_path,
                source=source,
                platform=platform,
                version=version,
                include_metadata=True,
            ),
            include_metadata=include_metadata,
        )
    if _OPERATION_ID_RE.fullmatch(stripped):
        operation_rows = get_endpoint_by_operation_id(
            stripped,
            limit=top_k,
            db_path=db_path,
            source=source,
            platform=platform,
            version=version,
            include_metadata=True,
        )
        if operation_rows:
            return _exact_endpoint_hits(
                operation_rows,
                include_metadata=include_metadata,
            )

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

    hits: dict[str, dict[str, Any]] = {}

    def add(
        row: dict[str, Any],
        *,
        kind: str,
        ref: str,
        text: str,
        score: int,
        exact: bool,
        identity: str | None = None,
    ) -> None:
        hit_key = identity or row.get("identity") or row.get("schema_identity")
        if not hit_key:
            hit_key = f"{row.get('source_family') or 'openapi_specs'}:{row['spec_file']}#{ref}"
        cur = hits.get(hit_key)
        if cur is None:
            hit = _hit_from_row(
                row,
                kind=kind,
                ref=ref,
                text=text,
                score=score,
                include_metadata=include_metadata,
            )
            hit["_exact"] = exact
            hits[hit_key] = hit
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
        for row in get_enum(
            term,
            limit=16,
            db_path=db_path,
            source=source,
            platform=platform,
            version=version,
            include_metadata=True,
        ):
            blob = (f"{row['spec_file']} {row['schema_name']} {row['path']} "
                    f"{row.get('description') or ''} {' '.join(map(str, row.get('enums') or []))} "
                    f"{row.get('enum_descriptions') or ''}")
            add(
                row,
                kind="enum",
                ref=f"{row['schema_name']}.{row['path']}",
                text=_fmt_enum_field(row),
                score=matched(blob),
                exact=True,
            )

    # 2. Exact endpoint lookups for hyphenated tokens (one progressive trim)
    for term in (g[0] for g in groups if "-" in g[0]):
        for candidate in (term, term.rsplit("-", 1)[0]):
            if "-" not in candidate:  # trimmed to a single bare word — too generic
                continue
            rows = get_endpoint(
                candidate,
                limit=6,
                db_path=db_path,
                source=source,
                platform=platform,
                version=version,
                include_metadata=True,
            )
            if rows:
                for row in rows:
                    blob = (f"{row['spec_file']} {row['method']} {row['path']} "
                            f"{row.get('summary') or ''} {row.get('description') or ''}")
                    add(
                        row,
                        kind="endpoint",
                        ref=f"{row['method']} {row['path']}",
                        text=_fmt_endpoint(row),
                        score=matched(blob),
                        exact=True,
                    )
                break

    # 3. FTS prefix search, re-ranked by distinct-concept coverage
    conn = connect(db_path)
    try:
        # Deep candidate fetch: bm25 penalizes long bodies, so with a shallow
        # cap the big multi-term schema rows (the ones that actually clear the
        # coverage threshold) get starved by hundreds of short single-term rows.
        match_expr = " OR ".join(f'"{s}"*' for s in flat)
        sql = (
            "SELECT kind, spec_file, ref, body, source_family, source_url, platform, "
            "version, spec_version, identity FROM fts WHERE fts MATCH ?"
        )
        params: list[Any] = [match_expr]
        clauses, clause_params = _metadata_filters(
            source=source,
            platform=platform,
            version=version,
        )
        if clauses:
            sql += " AND " + " AND ".join(clauses)
            params.extend(clause_params)
        sql += " ORDER BY bm25(fts) LIMIT 400"
        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            score = matched(f"{r['spec_file']} {r['body']}")
            if score < threshold:
                continue
            row = dict(r)
            if r["kind"] == "endpoint":
                method, _, path = r["ref"].partition(" ")
                ep = get_endpoint(
                    path,
                    method=method,
                    limit=1,
                    db_path=db_path,
                    source=source,
                    platform=platform,
                    version=version,
                    include_metadata=True,
                )
                text = _fmt_endpoint(ep[0]) if ep else r["ref"]
            else:
                # Schema hit: surface only the fields the query actually asked about
                fields = conn.execute(
                    "SELECT field_name, path, type, description, enums FROM fields "
                    "WHERE schema_identity = ? LIMIT 400",
                    (r["identity"],),
                ).fetchall()
                parts = []
                for f in fields:
                    enums = json.loads(f["enums"]) if f["enums"] else []
                    fblob = (
                        f"{f['field_name']} {f['description'] or ''} "
                        f"{' '.join(map(str, enums))}"
                    )
                    if matched(fblob):
                        enum_sfx = f" enum: {', '.join(map(str, enums[:24]))}" if enums else ""
                        parts.append(f"{f['path']} ({f['type']}){enum_sfx}")
                    if len(parts) >= 8:
                        break
                text = f"Schema {r['ref']} [{r['spec_file']}]: " + "; ".join(parts)
            add(
                row,
                kind=r["kind"],
                ref=r["ref"],
                text=text,
                score=score,
                exact=False,
                identity=str(r["identity"] or ""),
            )
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
