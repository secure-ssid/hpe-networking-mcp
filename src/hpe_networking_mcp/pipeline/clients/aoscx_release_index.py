"""Exact AOS-CX feature and release-note comparisons.

Feature Navigator snapshots are stored in the shared ``specs.sqlite`` artifact.
Release-note deltas are read from the embedded LanceDB corpus with exact source
and file-path filters; no embedding or semantic ranking is involved.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.pipeline.clients import lance_client
from hpe_networking_mcp.pipeline.clients.specs_index import DB_PATH

ROOT = repo_root()
SOURCES_DIR = ROOT / "ingestion" / "sources"
FEATURE_SOURCE_DIR = SOURCES_DIR / "feature_navigator"
MAX_LIMIT = 200
MAX_RELEASE_NOTE_ROWS = 5_000
MAX_EXCERPT_CHARS = 1_600

_PLATFORM_RE = re.compile(r"^(?:cx[\s_-]*)?([0-9]{4,5}[a-z0-9]*)$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_PATH_VERSION_RE = re.compile(r"/(\d{2}-\d{2}-\d{4})/")
_SAFE_PATH_RE = re.compile(r"^[a-z0-9_./-]+$")

_FEATURE_SCHEMA = """
DROP TABLE IF EXISTS aoscx_feature_releases;
DROP TABLE IF EXISTS aoscx_feature_support;
CREATE TABLE aoscx_feature_releases (
    platform TEXT NOT NULL,
    release TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    file_path TEXT NOT NULL,
    PRIMARY KEY (platform, release)
);
CREATE TABLE aoscx_feature_support (
    platform TEXT NOT NULL,
    release TEXT NOT NULL,
    feature_type TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    support TEXT NOT NULL,
    feature_publication_release TEXT,
    source_url TEXT NOT NULL,
    file_path TEXT NOT NULL,
    PRIMARY KEY (platform, release, feature_type, feature_name)
);
CREATE INDEX idx_aoscx_feature_platform_release
    ON aoscx_feature_support(platform, release);
"""

_SECTION_FILES: dict[str, tuple[str, ...]] = {
    "enhancements": ("enhancements.html",),
    "resolved_issues": ("resolved-issues.html",),
    "caveats": (
        "feature-caveats.html",
        "known-issues.html",
        "important-information.html",
    ),
}
_DEFAULT_SECTIONS = ("features", "enhancements", "resolved_issues", "caveats")


def _platform_key(platform: str) -> str:
    match = _PLATFORM_RE.fullmatch(platform.strip())
    if not match:
        raise ValueError("platform must look like '6100' or 'CX 6100'")
    return match.group(1).lower()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _validate_version(version: str, label: str) -> str:
    value = version.strip()
    if not _VERSION_RE.fullmatch(value):
        raise ValueError(f"{label} must look like '10.13' or '10.13.1000'")
    return value


def _resolve_version(available: list[str], requested: str, label: str) -> str:
    requested = _validate_version(requested, label)
    if requested in available:
        return requested
    prefix = ".".join(requested.split(".")[:2]) + "."
    family = sorted((item for item in available if item.startswith(prefix)), key=_version_key)
    if not family:
        raise ValueError(
            f"{label} {requested!r} is not indexed; available releases: "
            f"{', '.join(sorted(available, key=_version_key))}"
        )
    if len(requested.split(".")) == 2:
        return family[-1]
    requested_key = _version_key(requested)
    not_newer = [item for item in family if _version_key(item) <= requested_key]
    return not_newer[-1] if not_newer else family[0]


def _support(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Not documented"
    return value.strip()


def build_feature_index(
    sources_dir: Path = SOURCES_DIR,
    db_path: Path = DB_PATH,
) -> dict[str, int]:
    """Rebuild Feature Navigator history tables without touching other tables."""
    feature_dir = sources_dir / "feature_navigator"
    paths = sorted(feature_dir.glob("cx-*-history.json"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    counts = {"platforms": 0, "releases": 0, "feature_support": 0}
    try:
        conn.executescript(_FEATURE_SCHEMA)
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            try:
                platform = _platform_key(str(payload["product_name"]))
            except ValueError:
                # Feature Navigator also publishes "CX Simulator". It has no
                # hardware platform identifier and cannot be selected by this
                # platform comparison tool.
                continue
            product_id = int(payload["product_id"])
            product_name = str(payload["product_name"]).strip()
            source_url = str(payload["source_url"])
            releases = [str(item).strip() for item in payload["releases"]]
            file_path = f"feature_navigator/{path.name}"
            for release in releases:
                _validate_version(release, "release")
                conn.execute(
                    """
                    INSERT INTO aoscx_feature_releases
                    (platform, release, product_id, product_name, source_url, file_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (platform, release, product_id, product_name, source_url, file_path),
                )
                counts["releases"] += 1
            for feature in payload["features"]:
                feature_type = str(feature.get("feature_type") or "Other").strip()
                feature_name = str(feature.get("feature_name") or "").strip()
                if not feature_name:
                    continue
                support = feature.get("support") or {}
                for release in releases:
                    conn.execute(
                        """
                        INSERT INTO aoscx_feature_support
                        (platform, release, feature_type, feature_name, support,
                         feature_publication_release, source_url, file_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform,
                            release,
                            feature_type,
                            feature_name,
                            _support(support.get(release)),
                            feature.get("feature_publication_release"),
                            source_url,
                            file_path,
                        ),
                    )
                    counts["feature_support"] += 1
            counts["platforms"] += 1
        conn.commit()
    finally:
        conn.close()
    return counts


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"Structured RAG index not found at {db_path}; rebuild the indexes"
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM aoscx_feature_releases LIMIT 1")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise FileNotFoundError(
            "Feature Navigator history is not indexed; run "
            "`uv run python ingestion/scrape_feature_navigator.py` and rebuild "
            "the shared structured index"
        ) from exc
    return conn


def _feature_comparison(
    platform: str,
    from_version: str,
    to_version: str,
    *,
    db_path: Path,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        available = [
            row["release"]
            for row in conn.execute(
                "SELECT release FROM aoscx_feature_releases WHERE platform = ?",
                (platform,),
            )
        ]
        if not available:
            raise ValueError(f"platform {platform!r} is not indexed")
        baseline = _resolve_version(available, from_version, "from_version")
        target = _resolve_version(available, to_version, "to_version")
        if _version_key(baseline) >= _version_key(target):
            raise ValueError("from_version must resolve to a release older than to_version")
        rows = conn.execute(
            """
            SELECT release, feature_type, feature_name, support,
                   feature_publication_release, source_url, file_path
            FROM aoscx_feature_support
            WHERE platform = ? AND release IN (?, ?)
            ORDER BY feature_type, feature_name, release
            """,
            (platform, baseline, target),
        ).fetchall()
    finally:
        conn.close()

    by_feature: dict[tuple[str, str], dict[str, sqlite3.Row]] = {}
    for row in rows:
        by_feature.setdefault((row["feature_type"], row["feature_name"]), {})[
            row["release"]
        ] = row

    changes: list[dict[str, Any]] = []
    for (feature_type, feature_name), versions in by_feature.items():
        old = versions.get(baseline)
        new = versions.get(target)
        old_support = old["support"] if old else "Not documented"
        new_support = new["support"] if new else "Not documented"
        if old_support == new_support:
            continue
        citation = new or old
        changes.append(
            {
                "feature_type": feature_type,
                "feature_name": feature_name,
                "from": old_support,
                "to": new_support,
                "change": (
                    "added"
                    if old_support.lower() != "yes" and new_support.lower() == "yes"
                    else "removed"
                    if old_support.lower() == "yes" and new_support.lower() != "yes"
                    else "changed"
                ),
                "feature_publication_release": citation["feature_publication_release"],
                "file_path": citation["file_path"],
                "source_url": citation["source_url"],
            }
        )
    return {
        "resolved_from": baseline,
        "resolved_to": target,
        "total_changes": len(changes),
        "changes": changes,
    }


def _release_note_rows(platform: str) -> list[dict[str, Any]]:
    prefix = f"aoscx_release_notes/{platform}/"
    if not _SAFE_PATH_RE.fullmatch(prefix):
        raise ValueError("invalid release-note platform path")
    db = lance_client.connect()
    table = lance_client.docs_table(db)
    if table is None:
        raise FileNotFoundError(
            "Embedded docs index is missing; release notes come from the prose "
            "corpus, which this project does not publish — build it with "
            "`uv run python ingestion/ingest_docs.py`"
        )
    return (
        table.search()
        .where(
            "source = 'aoscx_release_notes' "
            f"AND file_path LIKE '{prefix}%'",
            prefilter=True,
        )
        .select(["file_path", "chunk_index", "text"])
        .limit(MAX_RELEASE_NOTE_ROWS)
        .to_list()
    )


def _release_note_comparison(
    platform: str,
    from_version: str,
    to_version: str,
    sections: tuple[str, ...],
) -> dict[str, Any]:
    rows = _release_note_rows(platform)
    available: set[str] = set()
    selected_suffixes = {
        suffix
        for section in sections
        for suffix in _SECTION_FILES.get(section, ())
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        path = str(row.get("file_path") or "")
        match = _PATH_VERSION_RE.search(path)
        if not match:
            continue
        version = match.group(1).replace("-", ".")
        available.add(version)
        suffix = path.rsplit("/", 1)[-1]
        if suffix in selected_suffixes:
            grouped.setdefault((version, path), []).append(row)
    releases = sorted(available, key=_version_key)
    baseline = _resolve_version(releases, from_version, "from_version")
    target = _resolve_version(releases, to_version, "to_version")
    if _version_key(baseline) >= _version_key(target):
        raise ValueError("from_version must resolve to a release older than to_version")

    entries: list[dict[str, Any]] = []
    suffix_to_section = {
        suffix: section for section, suffixes in _SECTION_FILES.items() for suffix in suffixes
    }
    for (version, path), chunks in sorted(
        grouped.items(), key=lambda item: (_version_key(item[0][0]), item[0][1])
    ):
        if not (_version_key(baseline) < _version_key(version) <= _version_key(target)):
            continue
        text = "\n".join(
            str(chunk.get("text") or "").strip()
            for chunk in sorted(chunks, key=lambda item: int(item.get("chunk_index") or 0))
            if str(chunk.get("text") or "").strip()
        )
        lowered = text.lower()
        if (
            "there are no enhancements" in lowered
            or "there are no resolved issues" in lowered
            or "there are no known issues" in lowered
        ):
            continue
        entries.append(
            {
                "release": version,
                "section": suffix_to_section[path.rsplit("/", 1)[-1]],
                "excerpt": text[:MAX_EXCERPT_CHARS],
                "file_path": path,
            }
        )
    return {
        "resolved_from": baseline,
        "resolved_to": target,
        "range_semantics": "from exclusive, to inclusive",
        "total_documents": len(entries),
        "entries": entries,
    }


def compare(
    platform: str,
    from_version: str,
    to_version: str,
    sections: list[str] | None = None,
    limit: int = 50,
    *,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Compare Feature Navigator support and release-note deltas exactly."""
    platform_key = _platform_key(platform)
    requested_sections = tuple(dict.fromkeys(sections or _DEFAULT_SECTIONS))
    invalid = sorted(set(requested_sections) - set(_DEFAULT_SECTIONS))
    if invalid:
        raise ValueError(
            "sections must contain only features, enhancements, "
            f"resolved_issues, or caveats; invalid: {', '.join(invalid)}"
        )
    bounded_limit = max(1, min(int(limit), MAX_LIMIT))
    result: dict[str, Any] = {
        "platform": f"CX {platform_key.upper()}",
        "requested_range": {"from": from_version, "to": to_version},
        "cumulative": False,
        "semantics": (
            "Feature Navigator compares endpoint snapshots. Release-note "
            "enhancements and resolved issues are per-release deltas."
        ),
        "sections": list(requested_sections),
        "errors": [],
    }

    combined: list[dict[str, Any]] = []
    if "features" in requested_sections:
        features = _feature_comparison(
            platform_key,
            from_version,
            to_version,
            db_path=db_path,
        )
        result["feature_versions"] = {
            "from": features["resolved_from"],
            "to": features["resolved_to"],
        }
        result["feature_change_count"] = features["total_changes"]
        combined.extend({"kind": "feature", **item} for item in features["changes"])

    note_sections = tuple(
        section for section in requested_sections if section in _SECTION_FILES
    )
    if note_sections:
        try:
            notes = _release_note_comparison(
                platform_key, from_version, to_version, note_sections
            )
            result["release_note_versions"] = {
                "from": notes["resolved_from"],
                "to": notes["resolved_to"],
            }
            result["range_semantics"] = notes["range_semantics"]
            result["release_note_document_count"] = notes["total_documents"]
            combined.extend({"kind": "release_note", **item} for item in notes["entries"])
        except FileNotFoundError as exc:
            result["errors"].append(str(exc))

    result["total_results"] = len(combined)
    result["results"] = combined[:bounded_limit]
    result["count"] = len(result["results"])
    result["limit"] = bounded_limit
    result["truncated"] = len(combined) > bounded_limit
    return result


if __name__ == "__main__":
    print(build_feature_index())
