"""Local, deterministic SKU and hardware-candidate catalog search.

This module deliberately keeps product discovery out of prose RAG.  A request
such as ``CX 6300 PoE 48 port SKU`` needs exact part numbers and a small,
explainable candidate list, not semantically similar documentation chunks.
The catalog is a SQLite database with an FTS5 candidate index and a normalised
SKU alias table.  It is built from a reviewed official-source snapshot by
``scripts/build_hardware_catalog.py`` and has no runtime network dependency.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hpe_networking_mcp._paths import repo_root

ROOT = repo_root()
SEED_PATH = ROOT / "ingestion" / "hardware_catalog_seed.json"
DB_PATH = ROOT / "data" / "hardware_catalog.sqlite"
BUILD_COMMAND = "python scripts/build_hardware_catalog.py"
MISSING_CATALOG_REMEDY = (
    f"build the local hardware catalog with `{BUILD_COMMAND}`; it uses the committed "
    "official-source seed and does not make a vendor API call at query time"
)

PARTIAL_COVERAGE_GUIDANCE = (
    "These are only the SKUs this catalog snapshot carries, not the complete product "
    "family. Coverage is partial, so do not present the list as exhaustive and do not "
    "add SKUs, specifications, or lifecycle claims from memory. Do not recommend or "
    "mention a different product family as a newer model, successor, or replacement "
    "unless it appears in these results. If the user needs the full family list, say "
    "the catalog snapshot is incomplete and point them at the official vendor source."
)

COMPARISON_GUIDANCE = (
    "Describe differences only from the fields under 'comparison'. Do not infer a "
    "distinction from the model name; for example do not claim a modular-versus-fixed "
    "difference unless the compared SKUs actually differ on that field. Do not "
    "introduce another product family as a successor or replacement."
)

_SCHEMA = """
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    family TEXT NOT NULL,
    device_type TEXT NOT NULL,
    port_count INTEGER,
    poe TEXT,
    uplinks TEXT,
    summary TEXT NOT NULL,
    specs_json TEXT NOT NULL,
    taa INTEGER NOT NULL DEFAULT 0,
    lifecycle_status TEXT NOT NULL,
    lifecycle_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_title TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    source_status TEXT NOT NULL
);
CREATE TABLE sku_aliases (
    alias TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES products(sku)
);
CREATE TABLE catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE VIRTUAL TABLE product_fts USING fts5(
    sku, vendor, brand, model, family, device_type, poe, uplinks, summary,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE INDEX idx_products_vendor ON products(vendor);
CREATE INDEX idx_products_device_type ON products(device_type);
CREATE INDEX idx_products_port_count ON products(port_count);
CREATE INDEX idx_aliases_sku ON sku_aliases(sku);
"""

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "i",
        "in",
        "is",
        "me",
        "need",
        "of",
        "please",
        "show",
        "sku",
        "the",
        "to",
        "what",
        "with",
    }
)
_TYPE_HINTS = frozenset(
    {"accessory", "ap", "firewall", "gateway", "optic", "power", "router", "switch"}
)
_CATALOG_HINTS = frozenset(
    {
        "candidate",
        "model",
        "ordering",
        "part",
        "poe",
        "port",
        "ports",
        "sku",
        "switch",
        "access point",
    }
)
_SKU_LIKE_RE = re.compile(r"\b(?:[A-Z]{1,4}\d{2,}[A-Z0-9-]*|[A-Z]{2,}\d[A-Z0-9-]*)\b", re.I)
_PORT_RE = re.compile(r"\b(\d{1,3})\s*(?:port|ports|p)\b", re.I)
_OFFICIAL_SOURCE_SUFFIXES = ("arubanetworks.com", "hpe.com", "juniper.net")


def _missing_catalog_error(db_path: Path | str) -> FileNotFoundError:
    return FileNotFoundError(f"hardware catalog missing at {db_path} — {MISSING_CATALOG_REMEDY}")


def normalize_sku(value: str) -> str:
    """Return case/punctuation-insensitive SKU form used for exact matching."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _tokens(value: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", value.casefold())
    tokens = [token for token in raw if token not in _STOP_WORDS]
    # Permit CX 6300 / EX 4400 input to find the compact family/SKU spellings.
    for left, right in zip(raw, raw[1:], strict=False):
        if left.isalpha() and right.isdigit():
            tokens.append(f"{left}{right}")
    return list(dict.fromkeys(tokens))


def _is_valid_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    required = (
        "sku",
        "vendor",
        "brand",
        "model",
        "family",
        "device_type",
        "summary",
        "source_url",
        "source_title",
        "snapshot_at",
    )
    if not all(isinstance(record.get(key), str) and record[key].strip() for key in required):
        return False
    parsed = urlparse(str(record["source_url"]))
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _OFFICIAL_SOURCE_SUFFIXES
    ):
        return False
    return str(record.get("source_status") or "verified").casefold() in {"verified", "stale"}


def _load_seed(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid hardware catalog seed {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"hardware catalog seed {path} must have schema_version 1")
    products = data.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError(f"hardware catalog seed {path} has no products")
    invalid = [index for index, record in enumerate(products) if not _is_valid_record(record)]
    if invalid:
        raise ValueError(
            f"hardware catalog seed {path} has invalid records at indexes {invalid[:5]}"
        )
    return data


def build(*, seed_path: Path = SEED_PATH, db_path: Path = DB_PATH) -> dict[str, int]:
    """Build a deterministic catalog index from a reviewed JSON snapshot."""
    seed = _load_seed(Path(seed_path))
    products = seed["products"]
    output = Path(db_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    seen_skus: set[str] = set()
    alias_count = 0
    try:
        conn = sqlite3.connect(temporary)
        try:
            conn.executescript(_SCHEMA)
            for record in sorted(products, key=lambda item: normalize_sku(str(item["sku"]))):
                sku = str(record["sku"]).strip().upper()
                if sku in seen_skus:
                    raise ValueError(f"duplicate hardware SKU {sku!r}")
                seen_skus.add(sku)
                specs = record.get("specs") if isinstance(record.get("specs"), dict) else {}
                lifecycle = (
                    record.get("lifecycle") if isinstance(record.get("lifecycle"), dict) else {}
                )
                status = str(record.get("lifecycle_status") or "unknown").strip().casefold()
                source_status = str(record.get("source_status") or "verified").strip().casefold()
                port_count = record.get("port_count")
                if not isinstance(port_count, int) or isinstance(port_count, bool):
                    port_count = None
                conn.execute(
                    (
                        "INSERT INTO products VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        sku,
                        str(record["vendor"]).strip().casefold(),
                        str(record["brand"]).strip(),
                        str(record["model"]).strip(),
                        str(record["family"]).strip(),
                        str(record["device_type"]).strip().casefold(),
                        port_count,
                        str(record.get("poe") or "").strip(),
                        str(record.get("uplinks") or "").strip(),
                        str(record["summary"]).strip(),
                        json.dumps(specs, sort_keys=True, separators=(",", ":")),
                        1 if record.get("taa") else 0,
                        status,
                        json.dumps(lifecycle, sort_keys=True, separators=(",", ":")),
                        str(record["source_url"]).strip(),
                        str(record["source_title"]).strip(),
                        str(record["snapshot_at"]).strip(),
                        source_status,
                    ),
                )
                aliases = {normalize_sku(sku)}
                aliases.update(
                    normalize_sku(str(alias))
                    for alias in record.get("aliases", [])
                    if isinstance(alias, str) and normalize_sku(alias)
                )
                for alias in sorted(aliases):
                    conn.execute("INSERT INTO sku_aliases(alias, sku) VALUES (?, ?)", (alias, sku))
                    alias_count += 1
                conn.execute(
                    """INSERT INTO product_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sku,
                        str(record["vendor"]).strip().casefold(),
                        str(record["brand"]).strip(),
                        str(record["model"]).strip(),
                        str(record["family"]).strip(),
                        str(record["device_type"]).strip().casefold(),
                        str(record.get("poe") or "").strip(),
                        str(record.get("uplinks") or "").strip(),
                        str(record["summary"]).strip(),
                    ),
                )
            metadata = {
                "schema_version": "1",
                "coverage": str(seed.get("coverage") or "partial"),
                "snapshot_at": str(seed.get("snapshot_at") or ""),
                "source_count": str(len(seed.get("sources") or [])),
                "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            conn.executemany("INSERT INTO catalog_meta(key, value) VALUES (?, ?)", metadata.items())
            conn.commit()
        finally:
            conn.close()
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"products": len(seen_skus), "aliases": alias_count}


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise _missing_catalog_error(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM catalog_meta")
    }


def _as_result(row: sqlite3.Row, *, include_specs: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sku": row["sku"],
        "vendor": row["vendor"],
        "brand": row["brand"],
        "model": row["model"],
        "family": row["family"],
        "device_type": row["device_type"],
        "port_count": row["port_count"],
        "poe": row["poe"] or None,
        "uplinks": row["uplinks"] or None,
        "summary": row["summary"],
        "taa": bool(row["taa"]),
        "lifecycle": {"status": row["lifecycle_status"], **json.loads(row["lifecycle_json"])},
        "source": {
            "url": row["source_url"],
            "title": row["source_title"],
            "snapshot_at": row["snapshot_at"],
            "status": row["source_status"],
        },
    }
    if include_specs:
        result["specs"] = json.loads(row["specs_json"])
    return result


def _candidate_rows(
    conn: sqlite3.Connection, tokens: list[str], vendor: str | None
) -> list[sqlite3.Row]:
    where = ""
    params: list[Any] = []
    if vendor:
        where = " WHERE p.vendor = ?"
        params.append(vendor)
    # An OR query protects model/port phrases from becoming an all-token FTS
    # miss. Ranking below remains deterministic and decides the final order.
    words = [token for token in tokens if len(token) > 1]
    if words:
        match = " OR ".join(f'"{word}"' for word in words[:16])
        sql = (
            "SELECT p.* FROM product_fts f JOIN products p ON p.sku = f.sku"
            f"{where}{' AND' if where else ' WHERE'} product_fts MATCH ? LIMIT 100"
        )
        try:
            rows = conn.execute(sql, [*params, match]).fetchall()
            if rows:
                return rows
        except sqlite3.OperationalError:
            # Malformed punctuation must never make a query endpoint fail.
            pass
    fallback_where = " WHERE vendor = ?" if vendor else ""
    return conn.execute(f"SELECT * FROM products{fallback_where} LIMIT 500", params).fetchall()


def _rank(row: sqlite3.Row, query: str, tokens: list[str], requested_port_count: int | None) -> int:
    haystack = " ".join(
        str(row[key] or "")
        for key in (
            "sku",
            "vendor",
            "brand",
            "model",
            "family",
            "device_type",
            "poe",
            "uplinks",
            "summary",
        )
    ).casefold()
    compact_haystack = normalize_sku(haystack)
    normalized_query = normalize_sku(query)
    score = 0
    if normalized_query and normalized_query == normalize_sku(str(row["sku"])):
        score += 1000
    elif normalized_query and len(normalized_query) >= 4 and normalized_query in compact_haystack:
        score += 60
    for token in tokens:
        token_in_haystack = token in haystack
        if token_in_haystack:
            score += 8
        compact = normalize_sku(token)
        is_compact_model = bool(re.fullmatch(r"[a-z]+\d+[a-z0-9]*", token))
        if len(compact) >= 4 and compact in compact_haystack and (
            not token_in_haystack or is_compact_model
        ):
            score += 10
    if requested_port_count is not None and row["port_count"] == requested_port_count:
        score += 40
    if "poe" in tokens and "poe" in haystack:
        score += 24
    if any(token in _TYPE_HINTS for token in tokens) and str(row["device_type"]) in tokens:
        score += 16
    return score


def is_catalog_query(query: str) -> bool:
    """True for SKU/model-selection language; not generic documentation questions."""
    lowered = query.casefold()
    tokens = _tokens(query)
    model_reference = bool(re.search(r"\b(?:cx|ex|ap|srx|mx|qfx)\s*-?\s*\d", lowered))
    explicit_selection = bool(
        set(tokens) & {"candidate", "ordering", "part", "sku"}
    )
    configuration_selection = model_reference and (
        "poe" in tokens or _PORT_RE.search(query) is not None
    )
    # A bare catalog-format identifier (JL665A, EX4400-48P) should take the
    # exact route, but a normal "CX6300 specs" question must retain the
    # existing full-series hardware-spec summary route.
    identifier_only = bool(_SKU_LIKE_RE.fullmatch(clean) if (clean := query.strip()) else False)
    return explicit_selection or configuration_selection or identifier_only


def _candidate_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    vendor: str | None = None,
    limit: int = 5,
    include_taa: bool = True,
) -> tuple[list[sqlite3.Row], list[str], int, int]:
    """Return bounded ranked matches plus any requested models absent from the catalog.

    The second element is non-empty when the query named a model/family the
    snapshot does not cover. Callers must not present the rows as answers in
    that case: the remaining candidates belong to a *different* family, and
    offering them silently is how a wrong product reaches a quote.

    The third element is the number of TAA variants withheld when
    ``include_taa`` is false.
    """
    tokens = _tokens(query)
    port_match = _PORT_RE.search(query)
    requested_port_count = int(port_match.group(1)) if port_match else None
    candidate_rows = _candidate_rows(conn, tokens, vendor)
    model_tokens = [
        normalize_sku(token)
        for token in tokens
        if re.fullmatch(r"(?:cx|ex|ap|srx|mx|qfx)\d+[a-z0-9]*", token)
    ]
    uncovered_models: list[str] = []
    if model_tokens:
        family_rows = [
            row
            for row in candidate_rows
            if any(
                token
                in normalize_sku(
                    " ".join(str(row[key] or "") for key in ("sku", "model", "family"))
                )
                for token in model_tokens
            )
        ]
        if family_rows:
            candidate_rows = family_rows
        else:
            # The snapshot is partial, so an unknown model is expected. Record
            # it instead of quietly widening back to every other family.
            uncovered_models = list(dict.fromkeys(model_tokens))
    ranked = sorted(
        (
            (_rank(row, query, tokens, requested_port_count), row)
            for row in candidate_rows
        ),
        key=lambda item: (-item[0], str(item[1]["sku"])),
    )
    # A one-word hit such as "appliance" is too weak to claim a product
    # candidate. Compact model tokens (CX6300, EX4400) are intentionally
    # allowed through at a lower bar: they are strong identifiers even
    # without a SKU suffix.
    model_token = any(re.fullmatch(r"[a-z]+\d+[a-z0-9]*", token) for token in tokens)
    minimum_score = 10 if model_token else 16
    kept = [row for score, row in ranked if score >= minimum_score]
    withheld_taa = 0
    if not include_taa:
        before = len(kept)
        kept = [row for row in kept if not row["taa"]]
        withheld_taa = before - len(kept)
    return kept[:limit], uncovered_models, withheld_taa, len(kept)


def search(
    query: str,
    *,
    vendor: str | None = None,
    include_specs: bool = False,
    include_taa: bool = False,
    limit: int = 5,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Search exact SKU aliases before returning bounded, ranked candidates.

    TAA variants are federal-procurement duplicates of the standard SKUs, so
    they are withheld unless ``include_taa`` is set. They are still counted and
    reported so the caller knows they exist.
    """
    clean_query = query.strip()
    if not clean_query:
        return {
            "ok": False,
            "error": "query must not be empty",
            "guidance": "Include a model, SKU, device type, port count, or PoE requirement.",
        }
    normalized_vendor = (
        vendor.strip().casefold() if isinstance(vendor, str) and vendor.strip() else None
    )
    if normalized_vendor and normalized_vendor not in {"aruba", "juniper"}:
        return {"ok": False, "error": "vendor must be 'aruba' or 'juniper'"}
    try:
        bounded_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        bounded_limit = 5
    try:
        conn = _connect(Path(db_path))
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "hint": MISSING_CATALOG_REMEDY}
    try:
        metadata = _metadata(conn)
        provenance = {
            "identity_authority": "official_vendor_source",
            "source_policy": "official Aruba/HPE/Juniper sources only",
            "coverage": metadata.get("coverage", "partial"),
            "catalog_snapshot_at": metadata.get("snapshot_at", ""),
        }
        normalized = normalize_sku(clean_query)
        exact = conn.execute(
            """SELECT p.* FROM sku_aliases a JOIN products p ON p.sku = a.sku
               WHERE a.alias = ? AND (? IS NULL OR p.vendor = ?)""",
            (normalized, normalized_vendor, normalized_vendor),
        ).fetchall()
        if exact:
            return {
                "ok": True,
                "match_type": "exact_sku",
                "results": [_as_result(exact[0], include_specs=include_specs)],
                "guidance": PARTIAL_COVERAGE_GUIDANCE,
                "catalog": metadata,
                "provenance": provenance,
            }
        matches, uncovered_models, withheld_taa, total_matches = _candidate_matches(
            conn, clean_query, vendor=normalized_vendor, limit=bounded_limit,
            include_taa=include_taa,
        )
        if uncovered_models:
            # Fail closed: the query named a model this snapshot does not
            # carry. Any remaining rows are a different family, so they are
            # returned as `related`, never as `results`.
            named = ", ".join(uncovered_models)
            return {
                "ok": False,
                "match_type": "model_not_in_catalog",
                "results": [],
                "requested_models": uncovered_models,
                "related": [
                    _as_result(row, include_specs=include_specs) for row in matches
                ],
                "guidance": (
                    f"{named} is not in this catalog snapshot "
                    f"(coverage: {metadata.get('coverage', 'partial')}). Any items under "
                    "'related' belong to a different product family and must not be "
                    "substituted. Confirm the model against the official vendor source."
                ),
                "catalog": metadata,
                "provenance": provenance,
            }
        if not matches:
            return {
                "ok": False,
                "match_type": "no_match",
                "results": [],
                "guidance": (
                    "No catalog match. Include the vendor, device type, model/family, and "
                    "port count or PoE requirement so the catalog can narrow candidates."
                ),
                "catalog": metadata,
                "provenance": provenance,
            }
        return {
            "ok": True,
            "match_type": "candidate" if len(matches) > 1 else "best_candidate",
            "results": [_as_result(row, include_specs=include_specs) for row in matches],
            "total_matches": total_matches,
            "returned": len(matches),
            "taa_variants_withheld": withheld_taa,
            "guidance": (
                PARTIAL_COVERAGE_GUIDANCE
                + (
                    f" Showing {len(matches)} of {total_matches} matches; raise `limit` "
                    "to see the rest."
                    if total_matches > len(matches)
                    else ""
                )
                + (
                    f" {withheld_taa} TAA federal-procurement variant(s) were withheld; "
                    "pass include_taa=true to list them."
                    if withheld_taa
                    else ""
                )
            ),
            "catalog": metadata,
            "provenance": provenance,
        }
    finally:
        conn.close()


def _resolve_comparison_device(
    conn: sqlite3.Connection, query: str
) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    """Resolve one device, refusing to select an arbitrary model variant."""
    clean_query = query.strip()
    if not clean_query:
        return None, {"query": query, "reason": "empty_identifier"}
    exact = conn.execute(
        """SELECT p.* FROM sku_aliases a JOIN products p ON p.sku = a.sku
           WHERE a.alias = ?""",
        (normalize_sku(clean_query),),
    ).fetchall()
    if exact:
        return exact[0], None
    candidates, uncovered_models, _, _ = _candidate_matches(conn, clean_query, limit=5)
    if uncovered_models:
        return None, {
            "query": clean_query,
            "reason": "model_not_in_catalog",
            "requested_models": uncovered_models,
        }
    if len(candidates) == 1:
        return candidates[0], None
    if candidates:
        return None, {
            "query": clean_query,
            "reason": "ambiguous_model",
            "candidates": [_as_result(row, include_specs=False) for row in candidates],
        }
    return None, {"query": clean_query, "reason": "no_match"}


def _comparison_fields(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Build source-backed, normalized side-by-side fields for resolved SKUs."""
    base_fields = ("device_type", "port_count", "poe", "uplinks", "lifecycle_status")
    spec_keys = sorted(
        {
            key
            for row in rows
            if row["source_status"] == "verified"
            for key in json.loads(row["specs_json"])
        }
    )
    fields: list[dict[str, Any]] = []
    for field in (*base_fields, *(f"specs.{key}" for key in spec_keys)):
        values: dict[str, Any] = {}
        for row in rows:
            sku = str(row["sku"])
            if row["source_status"] != "verified":
                values[sku] = "unknown"
                continue
            if field.startswith("specs."):
                value = json.loads(row["specs_json"]).get(field.removeprefix("specs."))
            else:
                value = row[field]
            values[sku] = value if value not in (None, "") else "unknown"
        fields.append(
            {
                "field": field,
                "values": values,
                "different": (
                    len({json.dumps(value, sort_keys=True) for value in values.values()}) > 1
                ),
            }
        )
    return fields


def compare(
    devices: list[str], *, db_path: Path = DB_PATH
) -> dict[str, Any]:
    """Compare two to five exact SKUs or unambiguous model identifiers.

    Ambiguous family/model names deliberately return candidates instead of
    silently selecting a variant. Every compared value comes from a verified
    catalog snapshot; unavailable or stale values are explicitly ``unknown``.
    """
    if not isinstance(devices, list) or not 2 <= len(devices) <= 5:
        return {"ok": False, "error": "devices must contain between 2 and 5 SKU or model values"}
    if not all(isinstance(device, str) for device in devices):
        return {"ok": False, "error": "each device must be a SKU or model string"}
    try:
        conn = _connect(Path(db_path))
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "hint": MISSING_CATALOG_REMEDY}
    try:
        metadata = _metadata(conn)
        resolved: list[sqlite3.Row] = []
        unresolved: list[dict[str, Any]] = []
        for device in devices:
            row, issue = _resolve_comparison_device(conn, device)
            if row is None:
                unresolved.append(issue or {"query": device, "reason": "no_match"})
            else:
                resolved.append(row)
        if unresolved:
            return {
                "ok": False,
                "match_type": "needs_selection",
                "unresolved": unresolved,
                "guidance": (
                    "Use an exact SKU for every ambiguous model before comparing devices; "
                    "the catalog will not choose a variant on your behalf."
                ),
                "catalog": metadata,
            }
        skus = [str(row["sku"]) for row in resolved]
        if len(set(skus)) != len(skus):
            return {
                "ok": False,
                "error": "devices must resolve to distinct SKUs",
                "resolved_skus": skus,
                "catalog": metadata,
            }
        stale_skus = [str(row["sku"]) for row in resolved if row["source_status"] != "verified"]
        result: dict[str, Any] = {
            "ok": True,
            "match_type": "comparison",
            "devices": [
                {
                    key: value
                    for key, value in _as_result(row, include_specs=False).items()
                    if key not in {"port_count", "poe", "uplinks", "lifecycle"}
                }
                for row in resolved
            ],
            "comparison": {"fields": _comparison_fields(resolved)},
            "guidance": COMPARISON_GUIDANCE,
            "catalog": metadata,
        }
        if stale_skus:
            result["warnings"] = [
                f"{', '.join(stale_skus)} use retained stale source snapshots; "
                "comparison values are unknown."
            ]
        return result
    finally:
        conn.close()


def format_compact_answer(result: dict[str, Any]) -> str:
    """Render bounded catalog results for ``ask_docs`` without a token-heavy dump."""
    if not result.get("ok"):
        return str(result.get("guidance") or result.get("error") or "No hardware catalog match.")
    lines = ["Hardware catalog results:"]
    for item in result.get("results", []):
        detail = []
        if item.get("port_count") is not None:
            detail.append(f"{item['port_count']} ports")
        if item.get("poe"):
            detail.append(str(item["poe"]))
        suffix = f" — {', '.join(detail)}" if detail else ""
        lines.append(f"- {item['sku']}: {item['model']}{suffix}")
    if result.get("match_type") == "candidate":
        lines.append("Provide the exact SKU to select one variant.")
    return "\n".join(lines)
