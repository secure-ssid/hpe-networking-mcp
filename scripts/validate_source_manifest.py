#!/usr/bin/env python3
"""Validate ingestion/source_manifest.json for drift/inconsistency.

Catches the kind of mistakes that break `ingestion/check_updates.py`,
`scripts/refresh_rag_sources.py`, and `ingestion/ingest_docs.py` silently:
a scraper path that no longer exists, an `output_dir` that doesn't follow
the `ingestion/sources/<source>` convention, a source missing from
`ingest_docs.py`'s `SOURCE_META`, or duplicate source keys.

Exits non-zero on any FAIL. Deferred scraper gaps must be explicitly
allowlisted below; unregistered scrapers or RAG doc_type mappings fail loudly.

Usage:
    python scripts/validate_source_manifest.py
    python scripts/validate_source_manifest.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "ingestion" / "source_manifest.json"
INGEST_DOCS_PATH = ROOT / "ingestion" / "ingest_docs.py"
RAG_PY_PATH = ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "rag.py"

REQUIRED_FIELDS = ("source", "doc_type", "purpose", "seed_urls", "output_dir", "notes")

# Declared source folders with no reproducible scraper yet. These sources are
# still useful in the manifest because manually exported files under
# ingestion/sources/<source> can be ingested, but the lack of a scraper is an
# explicit deferred implementation item rather than an unexplained warning.
SCRAPER_PENDING: dict[str, str] = {}

# Sources refreshed by one shared orchestrated scraper step in
# scripts/refresh_rag_sources.py instead of one per-source scraper command.
SHARED_SCRAPER: dict[str, str] = {
    "security_advisories": "ingestion/scrape_security_lifecycle.py",
    "lifecycle_notices": "ingestion/scrape_security_lifecycle.py",
    "juniper_lifecycle": "ingestion/scrape_security_lifecycle.py",
    "juniper_security_advisories": "ingestion/scrape_security_lifecycle.py",
}

#: Valid values for an entry's ``extra_script_phases`` map: "pre" runs the
#: script before its scraper (URL discovery), "post" after it.
EXTRA_SCRIPT_PHASES = ("pre", "post")


@dataclass(frozen=True)
class Check:
    status: str  # OK, WARN, FAIL
    name: str
    detail: str


def _literal_string_or_strings(node: ast.AST) -> str | tuple[str, ...] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        values: list[str] = []
        for item in node.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            values.append(item.value)
        return tuple(values)
    return None


def _load_dict_literal(path: Path, dict_name: str) -> dict[str, str | tuple[str, ...]] | None:
    """Return a top-level `dict_name = {...}` / `dict_name: T = {...}` string
    dict literal's contents, or None if the file/assignment can't be found or
    parsed as a plain string->string dict."""
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None

    for node in tree.body:
        target_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id == dict_name:
                target_node = node
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            t = node.target
            if isinstance(t, ast.Name) and t.id == dict_name:
                target_node = node
        if target_node is None or not isinstance(target_node.value, ast.Dict):
            continue
        result: dict[str, str | tuple[str, ...]] = {}
        for k, v in zip(target_node.value.keys, target_node.value.values):
            value = _literal_string_or_strings(v)
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and value is not None:
                result[k.value] = value
        return result
    return None


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def validate() -> list[Check]:
    checks: list[Check] = []

    if not MANIFEST_PATH.exists():
        return [Check("FAIL", "manifest exists", f"{MANIFEST_PATH} not found")]

    try:
        manifest = load_manifest()
    except json.JSONDecodeError as e:
        return [Check("FAIL", "manifest valid JSON", str(e))]

    if not isinstance(manifest, list):
        return [Check("FAIL", "manifest shape", "top-level value must be a JSON array")]

    checks.append(Check("OK", "manifest valid JSON", f"{len(manifest)} entries"))

    source_meta = _load_dict_literal(INGEST_DOCS_PATH, "SOURCE_META")
    doc_type_to_source = _load_dict_literal(RAG_PY_PATH, "_DOC_TYPE_TO_SOURCE")

    seen_sources: dict[str, int] = {}
    for i, entry in enumerate(manifest):
        label = entry.get("source", f"<entry #{i}>")

        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            checks.append(Check("FAIL", f"{label}: required fields", f"missing {missing}"))
            continue

        source = entry["source"]
        seen_sources[source] = seen_sources.get(source, 0) + 1

        expected_output_dir = f"ingestion/sources/{source}"
        if entry["output_dir"] != expected_output_dir:
            checks.append(Check(
                "FAIL", f"{source}: output_dir convention",
                f"expected {expected_output_dir!r}, got {entry['output_dir']!r}",
            ))
        else:
            checks.append(Check("OK", f"{source}: output_dir convention", "matches convention"))

        scraper = entry.get("scraper")
        if scraper is None:
            if source in SHARED_SCRAPER:
                shared_path = ROOT / SHARED_SCRAPER[source]
                if shared_path.exists():
                    checks.append(
                        Check("OK", f"{source}: shared scraper exists", SHARED_SCRAPER[source])
                    )
                else:
                    checks.append(
                        Check(
                            "FAIL",
                            f"{source}: shared scraper exists",
                            f"{SHARED_SCRAPER[source]} not found",
                        )
                    )
            elif source in SCRAPER_PENDING:
                checks.append(Check("OK", f"{source}: scraper pending", SCRAPER_PENDING[source]))
            else:
                checks.append(Check("FAIL", f"{source}: scraper", "no scraper registered"))
        else:
            scraper_path = ROOT / scraper
            if scraper_path.exists():
                checks.append(Check("OK", f"{source}: scraper exists", scraper))
            else:
                checks.append(Check("FAIL", f"{source}: scraper exists", f"{scraper} not found"))

        extras = entry.get("extra_scripts", [])
        for extra in extras:
            extra_path = ROOT / extra
            if not extra_path.exists():
                checks.append(Check("FAIL", f"{source}: extra_script exists", f"{extra} not found"))

        # extra_script_phases declares WHEN each extra script runs relative to
        # the scraper: "pre" (it builds the URL inventory the scraper reads)
        # or "post" (independent extractor). Unlisted scripts default to
        # "post", the historical behaviour.
        phases = entry.get("extra_script_phases", {})
        if not isinstance(phases, dict):
            checks.append(Check(
                "FAIL", f"{source}: extra_script_phases", "must be an object of script -> phase",
            ))
            phases = {}
        for script, phase in phases.items():
            if script not in extras:
                checks.append(Check(
                    "FAIL", f"{source}: extra_script_phases",
                    f"{script!r} is not listed in extra_scripts",
                ))
            if phase not in EXTRA_SCRIPT_PHASES:
                checks.append(Check(
                    "FAIL", f"{source}: extra_script_phases",
                    f"{script!r} has phase {phase!r}, expected one of {list(EXTRA_SCRIPT_PHASES)}",
                ))
        for extra in extras:
            # A discovery script writes the URL list its scraper then reads;
            # running it after the scrape refreshes nothing, so the manifest
            # must say so explicitly.
            if Path(extra).name.startswith("discover_") and phases.get(extra) != "pre":
                checks.append(Check(
                    "FAIL", f"{source}: extra_script_phases",
                    f"{extra} is a discovery script and must declare phase 'pre'",
                ))
        if extras and phases:
            checks.append(Check(
                "OK", f"{source}: extra_script_phases",
                ", ".join(f"{Path(k).name}={v}" for k, v in phases.items()),
            ))

        if source_meta is None:
            checks.append(
                Check(
                    "WARN",
                    f"{source}: SOURCE_META",
                    "could not parse ingest_docs.py SOURCE_META",
                )
            )
        elif source not in source_meta:
            checks.append(
                Check(
                    "FAIL",
                    f"{source}: SOURCE_META",
                    "missing from ingest_docs.py SOURCE_META",
                )
            )
        else:
            checks.append(
                Check(
                    "OK",
                    f"{source}: SOURCE_META",
                    f"doc_type={source_meta[source]!r}",
                )
            )

        doc_type = entry["doc_type"]
        if doc_type_to_source is None:
            checks.append(
                Check(
                    "WARN",
                    f"{source}: _DOC_TYPE_TO_SOURCE",
                    "could not parse rag.py _DOC_TYPE_TO_SOURCE",
                )
            )
        elif doc_type not in doc_type_to_source:
            checks.append(
                Check(
                    "FAIL",
                    f"{source}: _DOC_TYPE_TO_SOURCE",
                    f"doc_type {doc_type!r} not registered in rag.py",
                )
            )
        else:
            mapped = doc_type_to_source[doc_type]
            mapped_sources = {mapped} if isinstance(mapped, str) else set(mapped)
            if source not in mapped_sources:
                checks.append(
                    Check(
                        "FAIL",
                        f"{source}: _DOC_TYPE_TO_SOURCE",
                        f"doc_type {doc_type!r} maps to {sorted(mapped_sources)!r}, not {source!r}",
                    )
                )
            else:
                checks.append(Check("OK", f"{source}: _DOC_TYPE_TO_SOURCE", "registered"))

    dupes = {s: n for s, n in seen_sources.items() if n > 1}
    if dupes:
        checks.append(Check("FAIL", "duplicate source keys", f"{dupes}"))
    else:
        checks.append(Check("OK", "duplicate source keys", "none"))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = validate()

    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        for c in checks:
            print(f"[{c.status}] {c.name}: {c.detail}")

    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    if not args.json:
        print(f"\n{len(checks) - len(fails) - len(warns)} OK, {len(warns)} WARN, {len(fails)} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
