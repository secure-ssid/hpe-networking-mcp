"""Structured security-advisory and product-lifecycle lookup.

The prose RAG index remains useful for explanations, but CVEs, severity,
affected products, notice IDs, SKUs, and lifecycle dates need exact filters.
This module stores those fields beside the OpenAPI tables in ``specs.sqlite``
so the prebuilt release still has one structured SQLite artifact.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.pipeline.clients.specs_index import DB_PATH

ROOT = repo_root()
SOURCES_DIR = ROOT / "ingestion" / "sources"

SOURCE_DIRS = {
    "security_advisories": "security",
    "juniper_security_advisories": "security",
    "lifecycle_notices": "lifecycle",
    "juniper_lifecycle": "lifecycle",
}

_SCHEMA = """
DROP TABLE IF EXISTS advisories;
DROP TABLE IF EXISTS lifecycle_events;
DROP TABLE IF EXISTS knowledge_fts;
CREATE TABLE advisories (
    id INTEGER PRIMARY KEY,
    advisory_id TEXT,
    title TEXT NOT NULL,
    severity TEXT,
    status TEXT,
    initial_release TEXT,
    current_release TEXT,
    source_url TEXT,
    source_family TEXT NOT NULL,
    file_path TEXT NOT NULL,
    products TEXT,
    cves TEXT,
    body TEXT NOT NULL
);
CREATE TABLE lifecycle_events (
    id INTEGER PRIMARY KEY,
    notice_id TEXT,
    title TEXT NOT NULL,
    category TEXT,
    published TEXT,
    event_type TEXT,
    source_url TEXT,
    source_family TEXT NOT NULL,
    file_path TEXT NOT NULL,
    product_skus TEXT,
    replacement_skus TEXT,
    body TEXT NOT NULL
);
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    kind, ref, source_family, body
);
CREATE INDEX idx_advisory_id ON advisories(advisory_id);
CREATE INDEX idx_advisory_severity ON advisories(severity);
CREATE INDEX idx_lifecycle_notice_id ON lifecycle_events(notice_id);
"""

_SOURCE_RE = re.compile(r"<!--\s*source:\s*(.*?)\s*-->")
_BULLET_RE = re.compile(r"^- ([^:]+):\s*(.*)$")
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
_JSA_RE = re.compile(r"\bJSA\d{5,}\b", re.IGNORECASE)
_HPE_SKU_RE = re.compile(r"\b[A-Z]{1,2}\d{3,4}[A-Z](?:[A-Z0-9]{0,4})?\b")
_JUNIPER_SKU_RE = re.compile(
    r"\b(?:MIST-)?(?:AP|ME)[A-Z0-9]*(?:-[A-Z0-9]+)+\b|\bB-AP[A-Z0-9-]+\b"
)
_UPDATED_ON_RE = re.compile(r"Updated on\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_SEVERITY_RANK = {
    "unknown": 0,
    "none": 0,
    "low": 1,
    "medium": 2,
    "moderate": 2,
    "high": 3,
    "important": 3,
    "critical": 4,
}
_CHROME_TITLES = frozenset({"article detail", "juniper support portal - home", "home"})
_NAV_LINES = frozenset(
    {
        "skip to main content",
        "home",
        "knowledge",
        "quick links",
        "expand search",
        "log in",
        "knowledge base",
        "back",
        "print",
        "report a security vulnerability",
        "article detail",
    }
)
_SKIP_LIFECYCLE_STEMS = frozenset({"hpe-networking-lifecycle-policy"})
_LABEL_ALIASES = {
    "article id": "advisory id",
    "advisory id": "advisory id",
    "aggregate severity": "aggregate severity",
    "severity": "aggregate severity",
    "created": "initial release",
    "initial release": "initial release",
    "last updated": "current release",
    "current release": "current release",
    "status": "status",
    "notice id": "notice id",
    "product category": "product category",
    "published": "published",
    "product affected": "product affected",
}
_PRODUCT_NAME_HINTS = ("Juniper Apstra", "Mist Cloud", "Apstra", "Mist")
_UNLISTED_PRODUCT = "(no product listed)"


def _severity_rank(min_severity: str | None) -> int | None:
    """Validate + resolve a min_severity filter to its numeric floor.

    Returns None when no filter was requested. Raises ValueError for any
    non-empty value that is not one of low/medium/high/critical -- an
    unrecognized severity name fails closed rather than being ignored.
    """
    rank = _SEVERITY_RANK.get((min_severity or "unknown").strip().lower())
    if min_severity is not None and rank is None:
        raise ValueError("min_severity must be low, medium, high, or critical")
    return rank


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"Structured RAG index not found at {db_path}; run ingestion/ingest_docs.py"
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _title(text: str, fallback: str) -> str:
    heading = ""
    for line in text.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            break
    if heading and heading.casefold() not in _CHROME_TITLES:
        return heading
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        if stripped.casefold() in _NAV_LINES:
            continue
        if "security bulletin" in stripped.casefold() or len(stripped) > 40:
            return stripped
    return heading or fallback


def _source_url(text: str) -> str:
    match = _SOURCE_RE.search(text)
    return match.group(1).strip() if match else ""


def _store_label(values: dict[str, str], raw_label: str, raw_value: str) -> None:
    mapped = _LABEL_ALIASES.get(raw_label.strip().casefold())
    value = raw_value.strip()
    if not mapped or not value:
        return
    values.setdefault(mapped, value)
    if mapped == "current release":
        values.setdefault("published", value)


def _metadata(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines()]
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            values[match.group(1).strip().lower()] = match.group(2).strip()

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("#") or line.startswith("<!--"):
            index += 1
            continue
        if ":" in line:
            label, _, value = line.partition(":")
            if value.strip():
                _store_label(values, label, value)
                index += 1
                continue
            if index + 1 < len(lines) and lines[index + 1]:
                _store_label(values, label, lines[index + 1])
                index += 2
                continue
        if line.casefold() in _LABEL_ALIASES and index + 1 < len(lines):
            nxt = lines[index + 1]
            if nxt and nxt.casefold() not in _LABEL_ALIASES and not nxt.startswith("#"):
                _store_label(values, line, nxt)
                index += 2
                continue
        index += 1

    updated = _UPDATED_ON_RE.search(text)
    if updated:
        values.setdefault("published", updated.group(1))
    return values


def _section_bullets(text: str, heading: str) -> list[str]:
    values: list[str] = []
    active = False
    for line in text.splitlines():
        if line.strip() == f"## {heading}":
            active = True
            continue
        if active and line.startswith("## "):
            break
        if active and line.startswith("- "):
            values.append(line[2:].strip())
    return values


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _advisory_products(text: str, metadata: dict[str, str]) -> list[str]:
    products = _section_bullets(text, "Product catalog")
    if products:
        return products
    affected = metadata.get("product affected", "")
    if not affected:
        return []
    found = [name for name in _PRODUCT_NAME_HINTS if re.search(rf"\b{re.escape(name)}\b", affected)]
    return found or [affected]


def _advisory_id(metadata: dict[str, str], text: str, fallback: str) -> str:
    if metadata.get("advisory id"):
        return metadata["advisory id"]
    match = _JSA_RE.search(text)
    return match.group(0).upper() if match else fallback


def _parse_lifecycle_skus(text: str) -> tuple[list[str], list[str]]:
    products: set[str] = set()
    replacements: set[str] = set()
    for line in _section_bullets(text, "Affected and replacement products"):
        for part in line.split(";"):
            label, separator, value = part.partition(":")
            if not separator or not value.strip():
                continue
            normalized = label.strip().lower()
            if normalized == "product sku":
                products.add(value.strip())
            elif normalized == "replacement product sku":
                replacements.add(value.strip())
    if not products:
        products.update(_HPE_SKU_RE.findall(text))
        products.update(_JUNIPER_SKU_RE.findall(text))
    return sorted(products), sorted(replacements)


def build(
    sources_dir: Path = SOURCES_DIR,
    db_path: Path = DB_PATH,
) -> dict[str, int]:
    """Rebuild advisory/lifecycle tables without disturbing OpenAPI tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        sqlite3.connect(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    counts = {"advisories": 0, "lifecycle_events": 0, "skipped": 0}

    for source_family, kind in SOURCE_DIRS.items():
        source_dir = sources_dir / source_family
        if not source_dir.exists():
            continue
        for path in sorted(source_dir.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                counts["skipped"] += 1
                continue
            if not text:
                counts["skipped"] += 1
                continue
            if kind == "lifecycle" and path.stem in _SKIP_LIFECYCLE_STEMS:
                counts["skipped"] += 1
                continue
            relative = str(path.relative_to(sources_dir))
            title = _title(text, path.stem)
            metadata = _metadata(text)
            source_url = _source_url(text)

            if kind == "security":
                advisory_id = _advisory_id(metadata, text, path.stem)
                products = _advisory_products(text, metadata)
                cves = sorted({match.upper() for match in _CVE_RE.findall(text)})
                conn.execute(
                    """
                    INSERT INTO advisories (
                        advisory_id, title, severity, status, initial_release,
                        current_release, source_url, source_family, file_path,
                        products, cves, body
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        advisory_id,
                        title,
                        metadata.get("aggregate severity"),
                        metadata.get("status"),
                        metadata.get("initial release"),
                        metadata.get("current release"),
                        source_url,
                        source_family,
                        relative,
                        json.dumps(products),
                        json.dumps(cves),
                        text,
                    ),
                )
                conn.execute(
                    "INSERT INTO knowledge_fts VALUES (?, ?, ?, ?)",
                    ("advisory", advisory_id, source_family, text),
                )
                counts["advisories"] += 1
                continue

            product_skus, replacement_skus = _parse_lifecycle_skus(text)
            notice_id = metadata.get("notice id") or path.stem
            published = (
                metadata.get("published")
                or metadata.get("current release")
                or metadata.get("last updated")
            )
            conn.execute(
                """
                INSERT INTO lifecycle_events (
                    notice_id, title, category, published, event_type,
                    source_url, source_family, file_path, product_skus,
                    replacement_skus, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notice_id,
                    title,
                    metadata.get("product category"),
                    published,
                    "end-of-sale/end-of-life",
                    source_url,
                    source_family,
                    relative,
                    json.dumps(product_skus),
                    json.dumps(replacement_skus),
                    text,
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_fts VALUES (?, ?, ?, ?)",
                ("lifecycle", notice_id, source_family, text),
            )
            counts["lifecycle_events"] += 1

    conn.commit()
    conn.close()
    return counts


def _advisory_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["products"] = _json_list(result.pop("products", None))
    result["cves"] = _json_list(result.pop("cves", None))
    result.pop("body", None)
    return result


def lookup_advisories(
    *,
    product: str | None = None,
    cve: str | None = None,
    advisory_id: str | None = None,
    min_severity: str | None = None,
    limit: int = 20,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return exact advisory records filtered by identifiers and product text."""
    if not any((product, cve, advisory_id)):
        raise ValueError("provide product, cve, or advisory_id")
    limit = max(1, min(limit, 200))
    minimum = _severity_rank(min_severity)

    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("body LIKE ?")
        params.append(f"%{product}%")
    if cve:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(advisories.cves) AS item "
            "WHERE LOWER(CAST(item.value AS TEXT)) = LOWER(?))"
        )
        params.append(cve)
    if advisory_id:
        clauses.append("LOWER(advisory_id) = LOWER(?)")
        params.append(advisory_id)
    # clauses are static parameterized fragments ("col = ?"); every actual
    # value is bound through params below, never interpolated into sql.
    sql = "SELECT * FROM advisories WHERE " + " AND ".join(clauses)  # nosec B608
    sql += " ORDER BY current_release DESC, advisory_id DESC LIMIT ?"
    params.append(200 if minimum else limit)

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError(
            "Structured advisory index is missing; rebuild with ingestion/ingest_docs.py"
        ) from exc
    finally:
        conn.close()
    results = [_advisory_row(row) for row in rows]
    if minimum:
        results = [
            row
            for row in results
            if _SEVERITY_RANK.get(str(row.get("severity") or "").lower(), 0) >= minimum
        ]
    return results[:limit]


def lookup_lifecycle(
    product: str,
    *,
    limit: int = 20,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return lifecycle notices whose exact indexed text contains a product/SKU."""
    if not product.strip():
        raise ValueError("product must not be empty")
    limit = max(1, min(limit, 200))
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT notice_id, title, category, published, event_type,
                   source_url, source_family, file_path, product_skus,
                   replacement_skus
            FROM lifecycle_events
            WHERE body LIKE ?
            ORDER BY published DESC, notice_id DESC
            LIMIT ?
            """,
            (f"%{product}%", limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError(
            "Structured lifecycle index is missing; rebuild with ingestion/ingest_docs.py"
        ) from exc
    finally:
        conn.close()
    return [
        {
            **dict(row),
            "product_skus": _json_list(row["product_skus"]),
            "replacement_skus": _json_list(row["replacement_skus"]),
        }
        for row in rows
    ]


MAX_LIST_LIMIT = 200

# Known-format exact date parsing only. Covers the ISO instants the Aruba
# CSAF advisories use (only the leading YYYY-MM-DD is significant for a date
# *range*), the "Month D, YYYY" prose dates the legacy HPE lifecycle
# notices use, Juniper "D Mon YYYY" / "M/D/YYYY" table dates, and the
# "Updated on M/D/YYYY" stamp on the Aruba hardware EoS PDF. Anything else
# returns None — never guessed at — so it simply cannot participate in a
# since/until range filter.
_ISO_DATE_FMT = "%Y-%m-%d"
_DATE_FORMATS = (
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m/%d/%Y",
)


def _parse_exact_date(value: str | None) -> date | None:
    """Parse a known-exact date format; return None rather than guess."""
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.strptime(text[:10], _ISO_DATE_FMT).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _in_date_range(value: str | None, since: date | None, until: date | None) -> bool:
    if since is None and until is None:
        return True
    parsed = _parse_exact_date(value)
    if parsed is None:
        return False
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def _require_exact_date(label: str, value: str | None) -> date | None:
    if not value:
        return None
    parsed = _parse_exact_date(value)
    if parsed is None:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD date")
    return parsed


def list_advisories(
    *,
    product: str | None = None,
    cve: str | None = None,
    advisory_id: str | None = None,
    min_severity: str | None = None,
    source_family: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Bounded, filterable advisory listing — no identifier required.

    Unlike `lookup_advisories` (which requires at least one identifier),
    this lists/paginates across every advisory matching zero or more exact
    filters: product/model text, CVE, advisory ID, a severity floor, the
    authoritative `source_family`, and a `[since, until]` date range applied
    to `current_release` (falling back to `initial_release` when absent).

    Args:
        product: Product/model/version text contained in the advisory.
        cve: Exact CVE identifier, such as CVE-2025-13914.
        advisory_id: Exact vendor advisory ID, such as HPESBNW04987.
        min_severity: Optional low, medium, high, or critical threshold.
        source_family: Exact source authority, e.g. security_advisories or
            juniper_security_advisories.
        since: Inclusive lower-bound date (YYYY-MM-DD).
        until: Inclusive upper-bound date (YYYY-MM-DD).
        limit: Rows to return per page (default 20, range 1-200).
        offset: Rows to skip for pagination (default 0).

    Returns:
        A dict with `total_matched` (all rows meeting the filters, before
        pagination), `count` (rows on this page), `offset`, `limit`, and
        `results`.
    """
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    offset = max(0, offset)
    minimum = _severity_rank(min_severity)
    since_date = _require_exact_date("since", since)
    until_date = _require_exact_date("until", until)

    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("body LIKE ?")
        params.append(f"%{product}%")
    if cve:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(advisories.cves) AS item "
            "WHERE LOWER(CAST(item.value AS TEXT)) = LOWER(?))"
        )
        params.append(cve)
    if advisory_id:
        clauses.append("LOWER(advisory_id) = LOWER(?)")
        params.append(advisory_id)
    if source_family:
        clauses.append("source_family = ?")
        params.append(source_family)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # clauses are static parameterized fragments ("col = ?"); every actual
    # value is bound through params below, never interpolated into sql.
    sql = (
        f"SELECT * FROM advisories {where} "  # nosec B608
        "ORDER BY current_release DESC, advisory_id DESC"
    )

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError(
            "Structured advisory index is missing; rebuild with ingestion/ingest_docs.py"
        ) from exc
    finally:
        conn.close()

    results = [_advisory_row(row) for row in rows]
    if minimum is not None:
        results = [
            row
            for row in results
            if _SEVERITY_RANK.get(str(row.get("severity") or "").lower(), 0) >= minimum
        ]
    if since_date is not None or until_date is not None:
        results = [
            row
            for row in results
            if _in_date_range(
                row.get("current_release") or row.get("initial_release"),
                since_date,
                until_date,
            )
        ]

    total_matched = len(results)
    page = results[offset : offset + limit]
    return {
        "total_matched": total_matched,
        "count": len(page),
        "offset": offset,
        "limit": limit,
        "results": page,
    }


def list_lifecycle_events(
    *,
    product: str | None = None,
    product_sku: str | None = None,
    replacement_sku: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    source_family: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Bounded, filterable lifecycle-event listing — no identifier required.

    Args:
        product: Free-text product/model search across the indexed body.
        product_sku: Exact product SKU present in the record's parsed
            `product_skus` list (case-insensitive).
        replacement_sku: Exact replacement SKU present in the record's
            parsed `replacement_skus` list (case-insensitive).
        category: Exact product category, e.g. Switches or Wireless.
        event_type: Exact lifecycle event type/state, e.g.
            end-of-sale/end-of-life.
        source_family: Exact source authority, e.g. lifecycle_notices or
            juniper_lifecycle.
        since: Inclusive lower-bound date (YYYY-MM-DD) on `published`.
        until: Inclusive upper-bound date (YYYY-MM-DD) on `published`.
        limit: Rows to return per page (default 20, range 1-200).
        offset: Rows to skip for pagination (default 0).

    Returns:
        A dict with `total_matched`, `count`, `offset`, `limit`, and
        `results` (each with `product_skus`/`replacement_skus` as lists).
    """
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    offset = max(0, offset)
    since_date = _require_exact_date("since", since)
    until_date = _require_exact_date("until", until)

    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("body LIKE ?")
        params.append(f"%{product}%")
    if product_sku:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(lifecycle_events.product_skus) AS item "
            "WHERE LOWER(CAST(item.value AS TEXT)) = LOWER(?))"
        )
        params.append(product_sku)
    if replacement_sku:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(lifecycle_events.replacement_skus) AS item "
            "WHERE LOWER(CAST(item.value AS TEXT)) = LOWER(?))"
        )
        params.append(replacement_sku)
    if category:
        clauses.append("LOWER(category) = LOWER(?)")
        params.append(category)
    if event_type:
        clauses.append("LOWER(event_type) = LOWER(?)")
        params.append(event_type)
    if source_family:
        clauses.append("source_family = ?")
        params.append(source_family)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # clauses are static parameterized fragments ("col = ?"); every actual
    # value is bound through params below, never interpolated into sql.
    sql = (
        "SELECT notice_id, title, category, published, event_type, source_url, "
        "source_family, file_path, product_skus, replacement_skus "
        f"FROM lifecycle_events {where} ORDER BY published DESC, notice_id DESC"  # nosec B608
    )

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError(
            "Structured lifecycle index is missing; rebuild with ingestion/ingest_docs.py"
        ) from exc
    finally:
        conn.close()

    results = [
        {
            **dict(row),
            "product_skus": _json_list(row["product_skus"]),
            "replacement_skus": _json_list(row["replacement_skus"]),
        }
        for row in rows
    ]
    if since_date is not None or until_date is not None:
        results = [
            r for r in results if _in_date_range(r.get("published"), since_date, until_date)
        ]

    total_matched = len(results)
    page = results[offset : offset + limit]
    return {
        "total_matched": total_matched,
        "count": len(page),
        "offset": offset,
        "limit": limit,
        "results": page,
    }


def _normalize_evidence(value: str) -> str:
    """Normalize a product/SKU string for exact (non-fuzzy) equality only.

    Collapses whitespace and case-folds — no stemming, no synonyms, no
    partial/substring matching. Two strings correlate only when this
    normalization makes them equal.
    """
    return " ".join(value.strip().split()).casefold()


CORRELATION_MATCH_BASIS = (
    "normalized (case/whitespace only) exact string equality between an "
    "advisory's listed product text and a lifecycle record's "
    "product_skus/replacement_skus — not a fuzzy or semantic match"
)


def correlate_advisory_lifecycle(
    *,
    product: str | None = None,
    advisory_id: str | None = None,
    cve: str | None = None,
    limit: int = 20,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Link advisory product applicability to lifecycle records — exact only.

    For each matching advisory, every listed product/model string is
    normalized (case/whitespace only, see `_normalize_evidence`) and
    compared against every lifecycle record's normalized `product_skus` and
    `replacement_skus`. A hit is reported only when normalization makes the
    two byte-for-byte equal; there is no fuzzy/semantic scoring. An advisory
    product with no such match is reported under `unresolved_products`,
    never silently dropped or presented as "not affected".

    Args:
        product: Product/model/version text (forwarded to lookup_advisories).
        advisory_id: Exact vendor advisory ID.
        cve: Exact CVE identifier.
        limit: Advisories to correlate (default 20, range 1-200).

    Returns:
        `{"match_basis": ..., "advisories": [...]}` where each entry has
        `advisory_id`, `title`, `exact_matches` (bounded list of
        `{advisory_product, notice_id, matched_field, title, source_family,
        source_url, file_path}`), and `unresolved_products` (bounded list of
        advisory product strings with no exact lifecycle match found in
        these sources).
    """
    if not any((product, advisory_id, cve)):
        raise ValueError("provide product, advisory_id, or cve")
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    advisories = lookup_advisories(
        product=product, cve=cve, advisory_id=advisory_id, limit=limit, db_path=db_path
    )

    conn = _connect(db_path)
    try:
        lifecycle_rows = conn.execute(
            "SELECT notice_id, title, source_family, file_path, source_url, "
            "product_skus, replacement_skus FROM lifecycle_events"
        ).fetchall()
    finally:
        conn.close()

    sku_index: dict[str, list[dict[str, Any]]] = {}
    for row in lifecycle_rows:
        record = dict(row)
        for field_name in ("product_skus", "replacement_skus"):
            for sku in _json_list(record[field_name]):
                key = _normalize_evidence(sku)
                sku_index.setdefault(key, []).append(
                    {
                        "notice_id": record["notice_id"],
                        "title": record["title"],
                        "source_family": record["source_family"],
                        "file_path": record["file_path"],
                        "source_url": record["source_url"],
                        "matched_field": field_name.rstrip("s"),
                    }
                )

    correlated: list[dict[str, Any]] = []
    for advisory in advisories:
        exact_matches: list[dict[str, Any]] = []
        unresolved: list[str] = []
        products = advisory.get("products") or []
        if not products:
            unresolved.append(_UNLISTED_PRODUCT)
        for prod in products:
            hits = sku_index.get(_normalize_evidence(prod))
            if hits:
                for hit in hits[:10]:
                    exact_matches.append({"advisory_product": prod, **hit})
            else:
                unresolved.append(prod)
        correlated.append(
            {
                "advisory_id": advisory.get("advisory_id"),
                "title": advisory.get("title"),
                "exact_matches": exact_matches[:MAX_LIST_LIMIT],
                "unresolved_products": unresolved[:MAX_LIST_LIMIT],
            }
        )

    return {"match_basis": CORRELATION_MATCH_BASIS, "advisories": correlated}


_ADVISORY_CITATION_FIELDS: tuple[str, ...] = (
    "advisory_id",
    "source_url",
    "severity",
    "current_release",
)
_LIFECYCLE_CITATION_FIELDS: tuple[str, ...] = (
    "notice_id",
    "source_url",
    "published",
    "product_skus",
)


def citation_completeness(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Report, per source authority, how complete each table's citation fields are.

    Counts non-null/non-empty values for the fields `ask_docs`/
    `lookup_advisory`/`check_product_lifecycle` cite: advisory `advisory_id`/
    `source_url`/`severity`/`current_release`, and lifecycle `notice_id`/
    `source_url`/`published`/`product_skus`. This makes an indexing gap
    (e.g. a source family whose bullet-metadata parser finds no matches
    because that source renders as a table rather than bullets) visible as
    a completeness ratio instead of a silent None. Never returns raw
    `body` text.
    """
    conn = _connect(db_path)
    try:
        try:
            advisory_rows = conn.execute(
                "SELECT source_family, advisory_id, source_url, severity, "
                "current_release FROM advisories"
            ).fetchall()
            lifecycle_rows = conn.execute(
                "SELECT source_family, notice_id, source_url, published, "
                "product_skus FROM lifecycle_events"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise FileNotFoundError(
                "Structured advisory/lifecycle index is missing; "
                "rebuild with ingestion/ingest_docs.py"
            ) from exc
    finally:
        conn.close()

    def _tally(rows: list[sqlite3.Row], fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for row in rows:
            family = row["source_family"]
            bucket = totals.setdefault(family, {"total": 0, **{f: 0 for f in fields}})
            bucket["total"] += 1
            for f in fields:
                value = row[f]
                if f == "product_skus":
                    if _json_list(value):
                        bucket[f] += 1
                elif value not in (None, ""):
                    bucket[f] += 1
        return totals

    return {
        "advisories": _tally(advisory_rows, _ADVISORY_CITATION_FIELDS),
        "lifecycle_events": _tally(lifecycle_rows, _LIFECYCLE_CITATION_FIELDS),
    }
