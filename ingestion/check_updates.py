#!/usr/bin/env python3
"""Check RAG doc sources for upstream content changes.

Tiered "smart" freshness check per `ingestion/source_manifest.json` source:

1. Resolve the source's known URL list:
   - `url_seed_file` in the manifest (absolute list), when present.
   - Otherwise scan already-scraped files under `output_dir` for the
     `<!-- source: URL -->` header that `scrape.py`, `scrape_nac_docs.py`,
     `scrape_vsg.py`, and `scrape_techdocs_pw.py` all write.
   - `openapi_specs` and `product_specs` are resolved from the committed
     registry/api-next manifests already in this repo
     (`ingestion/openapi_registry_manifest.json` reference pages plus their
     ReadMe api-registry documents, and `ingestion/product_specs_manifest.json`
     reference pages). The pre-July-2026 behaviour -- rebuilding
     `internal-ui.central.arubanetworks.com/cnxconfig/docs/<stem>.json`
     download URLs from local filenames -- was removed: that host was retired
     with the ReadMe SuperHub migration, so every one of those "checks" was
     really a dead-host error being counted as a freshness signal.
   - Sources with no resolvable URL list (e.g. `aos_techdocs`, whose URL
     inventory only lives in ephemeral `/tmp/aos_*_urls.json` from
     `discover_aos_urls.py`, or `feature_navigator`, which has no scraper
     yet) are reported as `unresolvable` with next-step guidance instead of
     silently skipped.

2. Per URL, do a single conditional GET carrying any stored `ETag` /
   `Last-Modified` validator (`If-None-Match` / `If-Modified-Since`). A 304
   response is the cheap path — no body re-fetch, marked unchanged. Any other
   response falls back to a SHA-256 content-hash compare against the last
   stored hash — this is the authoritative check for sites with unstable or
   absent validators (many docs/marketing sites don't implement conditional
   GET correctly).

3. Sites that block plain HTTP clients (403/406, several already documented
   in the scrapers as needing Playwright) are reported as `blocked` — they
   need a Playwright-based re-scrape to check, which this script does not
   drive automatically (keeps this script fast/dependency-light). Use
   `scripts/refresh_rag_sources.py` to re-run the real Playwright scraper on
   a longer cadence for those sources regardless of this check's result.

State persists in `data/source_state.sqlite` via
`hpe_networking_mcp.pipeline.clients.source_state`.

Pacing: this touches every known URL of a source, which for the larger sources
now means thousands of requests, so it is throttled like the scrapers it backs
(`CHECK_WORKERS` × `REQUEST_DELAY`). A full-corpus check is a handful of
minutes, not seconds — that is intentional, and it is why the scheduled job in
`scripts/schedule_freshness_check.sh` runs weekly rather than hourly.

Usage:
    python ingestion/check_updates.py                  # check all sources
    python ingestion/check_updates.py --source vsg_docs # one source
    python ingestion/check_updates.py --dry-run         # report only, no state writes
    python ingestion/check_updates.py --json            # machine-readable report only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402
from hpe_networking_mcp.pipeline.clients.source_state import (  # noqa: E402
    SourceStateStore,
    get_store,
)
from ingestion.readme_registry import REGISTRY_BASE_URL  # noqa: E402

MANIFEST_PATH = ROOT / "ingestion" / "source_manifest.json"
SOURCES_DIR = ROOT / "ingestion" / "sources"
REGISTRY_MANIFEST_PATH = ROOT / "ingestion" / "openapi_registry_manifest.json"
PRODUCT_SPECS_MANIFEST_PATH = ROOT / "ingestion" / "product_specs_manifest.json"
CHECK_NAME = "rag_source_freshness"
DEFAULT_ARTIFACT_PATH = ROOT / "outputs" / "drift" / "rag-source-freshness.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
REQUEST_TIMEOUT = 15
CHECK_WORKERS = 4
# Freshness checks hit every known URL of a source, and the corpus now runs to
# thousands of pages per source, so this is a crawl in its own right and is
# paced like one. Matches scrape_mist_docs.py (4 workers / 0.4s), giving ~10
# req/s worst case instead of an unthrottled 10-wide burst.
REQUEST_DELAY = 0.4
SOURCE_HEADER_RE = re.compile(r"<!--\s*source:\s*(\S+)\s*-->")

# RETIRED: internal-ui.central.arubanetworks.com/cnxconfig/docs was the
# pre-migration spec download origin. It stopped resolving with the July 2026
# ReadMe SuperHub migration (see ingestion/readme_registry.py), so
# reconstructing URLs from local filenames produced nothing but dead-host
# errors. Spec URLs now come from the committed registry/api-next manifests
# below -- the same resolvers ingestion and the drift gates already use.

#: Per-URL statuses mapped onto the shared drift taxonomy. A blocked or
#: unreachable page is `unavailable`, never `content_drift`.
_RESULT_CLASS_BY_STATUS = {
    "unchanged": taxonomy.FRESH,
    "changed": taxonomy.CONTENT_DRIFT,
    "new": taxonomy.SOURCE_ADDED,
    "baseline": taxonomy.NOT_CHECKED,
    "gone": taxonomy.SOURCE_REMOVED,
    "blocked": taxonomy.UNAVAILABLE,
    "error": taxonomy.UNAVAILABLE,
    "skipped": taxonomy.NOT_CHECKED,
}


def load_manifest() -> list[dict[str, Any]]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _urls_from_headers(output_dir: Path) -> list[str]:
    """Scan already-scraped text files for the `<!-- source: URL -->` header."""
    if not output_dir.exists():
        return []
    urls: list[str] = []
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".md", ".htm", ".html", ".txt"):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:500]
        except Exception:
            continue
        m = SOURCE_HEADER_RE.search(head)
        if m:
            urls.append(m.group(1))
    return urls


def _urls_from_seed_file(seed_file: str, seed_urls: list[str]) -> list[str]:
    """Load an absolute or site-relative path list and resolve against seed_urls[0]."""
    seed_path = ROOT / seed_file
    if not seed_path.exists():
        return []
    paths = json.loads(seed_path.read_text(encoding="utf-8"))
    if not paths:
        return []
    if paths[0].startswith("http"):
        return list(paths)
    base = seed_urls[0] if seed_urls else ""
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return [urljoin(origin, quote(p)) for p in paths]


def _load_json(path: Path) -> Any:
    """Read a committed manifest, returning None when it is missing/corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def urls_from_registry_manifest(
    manifest_path: Path | None = None,
) -> tuple[list[str], str | None]:
    """Resolve `openapi_specs` URLs from the committed ReadMe registry manifest.

    Two URLs per registry, both current resolvers (never the retired
    internal-ui host):

    * the developer-portal **reference page**, whose HTML carries the
      ``oasPublicUrl`` pointer — a change here is how a portal/layout move
      gets spotted; and
    * the **api-registry document** that pointer resolves to
      (``dash.readme.com/api/v1/api-registry/<id>``), whose body is the spec
      itself.
    """
    manifest_path = manifest_path or REGISTRY_MANIFEST_PATH
    data = _load_json(manifest_path)
    if not isinstance(data, dict):
        return [], (
            f"{manifest_path.name} is missing or unreadable — run "
            "ingestion/scrape_openapi.py (and scrape_cnac_spec.py) to build it"
        )
    registries = data.get("registries")
    if not isinstance(registries, dict) or not registries:
        return [], (
            f"no registries recorded in {manifest_path.name} — run "
            "ingestion/scrape_openapi.py at least once to establish a baseline"
        )
    urls: list[str] = []
    for registry_id, entry in sorted(registries.items()):
        if not isinstance(entry, dict):
            continue
        source_url = entry.get("source_url")
        if source_url:
            urls.append(str(source_url))
        urls.append(f"{REGISTRY_BASE_URL}/{registry_id}")
    return sorted(set(urls)), None


def urls_from_product_specs_manifest(
    manifest_path: Path | None = None,
) -> tuple[list[str], str | None]:
    """Resolve `product_specs` URLs from the committed api-next manifest.

    ``ingestion/product_specs_manifest.json`` records one reference page per
    harvested spec (``scrape_apinext_specs.py`` writes it), so the api-next
    sections get the same first-class freshness coverage the ReadMe
    registries have. Deeper, locally-derivable product-spec assertions
    (branch/spec_uri/path_count/digest/sidebar membership) live in
    ``scripts/check_product_spec_freshness.py``.
    """
    manifest_path = manifest_path or PRODUCT_SPECS_MANIFEST_PATH
    data = _load_json(manifest_path)
    specs = data.get("specs") if isinstance(data, dict) else None
    if not isinstance(specs, list) or not specs:
        return [], (
            f"no specs recorded in {manifest_path.name} — run "
            "ingestion/scrape_apinext_specs.py at least once to establish a baseline"
        )
    urls = [
        str(entry["source_url"])
        for entry in specs
        if isinstance(entry, dict) and entry.get("source_url")
    ]
    if not urls:
        return [], f"{manifest_path.name} records no source_url values"
    return sorted(set(urls)), None


#: Sources whose URL inventory comes from a committed manifest resolver
#: rather than from scraped-file headers or a seed file.
MANIFEST_RESOLVERS = {
    "openapi_specs": urls_from_registry_manifest,
    "product_specs": urls_from_product_specs_manifest,
}


def resolve_urls(entry: dict[str, Any]) -> tuple[list[str], str | None]:
    """Return (urls, unresolvable_reason). Empty urls + reason means skip with guidance."""
    source = entry["source"]
    output_dir = ROOT / entry["output_dir"]

    resolver = MANIFEST_RESOLVERS.get(source)
    if resolver is not None:
        urls, reason = resolver()
        if urls:
            return urls, None
        return [], reason

    if entry.get("url_seed_file"):
        urls = _urls_from_seed_file(entry["url_seed_file"], entry.get("seed_urls", []))
        if urls:
            return urls, None

    urls = _urls_from_headers(output_dir)
    if urls:
        return urls, None

    if not entry.get("scraper"):
        return [], (
            "no scraper registered for this source yet — see notes in "
            "source_manifest.json, or scaffold one with scripts/add_rag_source.py"
        )

    return [], (
        f"no known URL inventory found under {entry['output_dir']} — run "
        f"{entry.get('scraper')} at least once to establish a baseline"
    )


def check_url(
    url: str,
    source: str,
    store: SourceStateStore,
    dry_run: bool,
    *,
    baseline_exists: bool = True,
) -> dict[str, Any]:
    """Conditionally GET one URL and classify the outcome.

    Statuses and their taxonomy classes (see ``_RESULT_CLASS_BY_STATUS``):

    * ``unchanged`` (304 or identical hash) -> ``fresh``
    * ``changed`` (hash differs from the stored one) -> ``content_drift``
    * ``new`` (no stored hash, but this source already had a baseline)
      -> ``source_added``
    * ``baseline`` (no stored hash and the source had no state at all --
      this run is only establishing the baseline) -> ``not_checked``
    * ``gone`` (404/410) -> ``source_removed``
    * ``blocked`` (401/403/406/429) -> ``unavailable``
    * ``error`` (any other HTTP/transport failure) -> ``unavailable``

    The last three never become ``content_drift``: a site that blocks us or
    an origin that times out says nothing about whether the content moved.
    """
    time.sleep(REQUEST_DELAY)
    prior = store.get(url)
    headers = {"User-Agent": UA}
    if prior:
        if prior["etag"]:
            headers["If-None-Match"] = prior["etag"]
        if prior["last_modified"]:
            headers["If-Modified-Since"] = prior["last_modified"]

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read()
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            if not dry_run and prior:
                store.record_checked(
                    url, source,
                    etag=prior["etag"], last_modified=prior["last_modified"],
                    content_hash=prior["content_hash"], changed=False,
                )
            return _url_result(url, "unchanged", method="metadata_304")
        if e.code in (404, 410):
            return _url_result(url, "gone", method="http_gone", detail=f"HTTP {e.code}")
        if e.code in (401, 403, 406, 429):
            return _url_result(
                url, "blocked", method="http_blocked", detail=f"HTTP {e.code}"
            )
        return _url_result(url, "error", method="http_error", detail=f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 — report any fetch failure, don't crash the run
        return _url_result(url, "error", method="transport_error", detail=str(e))

    content_hash = hashlib.sha256(body).hexdigest()
    prior_hash = prior["content_hash"] if prior else None
    changed = prior_hash is None or content_hash != prior_hash
    if prior_hash is None:
        status = "new" if baseline_exists else "baseline"
    else:
        status = "changed" if changed else "unchanged"

    if not dry_run:
        store.record_checked(
            url, source, etag=etag, last_modified=last_modified,
            content_hash=content_hash, changed=changed,
        )
    return _url_result(url, status, method="content_hash")


def _url_result(url: str, status: str, *, method: str, detail: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "method": method,
        "result_class": _RESULT_CLASS_BY_STATUS[status],
    }
    if detail:
        result["detail"] = detail
    return result


def check_source(entry: dict[str, Any], store: SourceStateStore, dry_run: bool,
                  progress: bool = False, offline: bool = False) -> dict[str, Any]:
    source = entry["source"]
    urls, unresolvable_reason = resolve_urls(entry)
    if unresolvable_reason:
        return {
            "source": source, "resolvable": False, "reason": unresolvable_reason,
            "result_class": taxonomy.NOT_CHECKED,
            "checked": 0, "new": 0, "changed": 0, "unchanged": 0, "baseline": 0,
            "gone": 0, "blocked": 0, "errors": 0, "urls": [],
            "class_counts": dict.fromkeys(taxonomy.RESULT_CLASSES, 0),
        }

    if offline:
        return {
            "source": source, "resolvable": True,
            "reason": "offline: URL inventory resolved, nothing fetched",
            "result_class": taxonomy.NOT_CHECKED,
            "checked": 0, "new": 0, "changed": 0, "unchanged": 0, "baseline": 0,
            "gone": 0, "blocked": 0, "errors": 0,
            "known_urls": len(urls), "urls": [],
            "class_counts": dict.fromkeys(taxonomy.RESULT_CLASSES, 0),
        }

    if progress:
        print(f"  checking {source} ({len(urls)} urls)...", file=sys.stderr, flush=True)

    baseline_exists = store.known_url_count(source) > 0
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        futures = {
            pool.submit(check_url, u, source, store, dry_run, baseline_exists=baseline_exists): u
            for u in urls
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    counts = {
        "new": 0, "changed": 0, "unchanged": 0, "baseline": 0,
        "gone": 0, "blocked": 0, "errors": 0,
    }
    class_counts = dict.fromkeys(taxonomy.RESULT_CLASSES, 0)
    for r in results:
        key = {"error": "errors"}.get(r["status"], r["status"])
        if key in counts:
            counts[key] += 1
        class_counts[r["result_class"]] += 1
    return {
        "source": source, "resolvable": True, "reason": None,
        "result_class": source_result_class(class_counts),
        "checked": len(urls), **counts, "urls": results,
        "class_counts": class_counts,
    }


def source_result_class(class_counts: dict[str, int]) -> str:
    """Collapse a source's per-URL classes into one class by taxonomy precedence."""
    for candidate in taxonomy.EXIT_PRECEDENCE:
        if class_counts.get(candidate):
            return candidate
    if class_counts.get(taxonomy.FRESH):
        return taxonomy.FRESH
    return taxonomy.NOT_CHECKED


def build_report(reports: list[dict[str, Any]], *, offline: bool,
                  exit_code_mode: str = "classified") -> dict[str, Any]:
    """Render per-source reports as a shared-taxonomy drift report."""
    findings = [
        taxonomy.Finding(
            target=r["source"],
            result_class=r["result_class"],
            detail=r["reason"] or (
                f"checked={r['checked']} unchanged={r['unchanged']} changed={r['changed']} "
                f"new={r['new']} gone={r['gone']} blocked={r['blocked']} errors={r['errors']}"
            ),
            evidence={
                "checked": r["checked"],
                "changed": r["changed"],
                "new": r["new"],
                "gone": r["gone"],
                "blocked": r["blocked"],
                "errors": r["errors"],
                "known_urls": r.get("known_urls"),
            },
        )
        for r in reports
    ]
    return taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=not offline,
        exit_code_mode=exit_code_mode,
        notes=(
            "Per-source RAG freshness. openapi_specs/product_specs URLs come from "
            "the committed registry/api-next manifests; the retired internal-ui "
            "host is no longer reconstructed. Blocked/unreachable pages are "
            "unavailable, never content drift."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Check only this manifest source")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report only — do not write to source_state.sqlite")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Resolve each source's URL inventory but fetch nothing; every source "
            "is reported not_checked (never fresh)."
        ),
    )
    parser.add_argument(
        "--json-artifact",
        type=Path,
        default=None,
        help="Also write the shared-taxonomy drift report to this path.",
    )
    parser.add_argument(
        "--exit-code-mode",
        choices=taxonomy.EXIT_CODE_MODES,
        default="legacy",
        help=(
            "legacy (default, keeps refresh_rag_sources.py's contract): always exit 0 "
            "on a completed run; classified: one exit code per dominant result class."
        ),
    )
    args = parser.parse_args()

    manifest = load_manifest()
    if args.source:
        manifest = [e for e in manifest if e["source"] == args.source]
        if not manifest:
            parser.error(f"unknown source: {args.source!r}")

    store = get_store()
    reports = [
        check_source(entry, store, args.dry_run, progress=not args.json, offline=args.offline)
        for entry in manifest
    ]

    changed_sources = [
        r["source"] for r in reports if r["resolvable"] and (r["new"] or r["changed"])
    ]
    report = build_report(reports, offline=args.offline, exit_code_mode=args.exit_code_mode)

    if args.json_artifact:
        taxonomy.write_report(args.json_artifact, report)

    if args.json:
        print(json.dumps({
            "sources": reports,
            "changed_sources": changed_sources,
            "drift_report": report,
        }, indent=2))
        return 0 if args.exit_code_mode == "legacy" else int(report["exit_code"])

    for r in reports:
        if not r["resolvable"]:
            print(f"[SKIP]     {r['source']}: {r['reason']}")
            continue
        if args.offline:
            print(f"[OFFLINE]  {r['source']}: {r.get('known_urls', 0)} known urls, not fetched")
            continue
        flag = "CHANGED" if (r["new"] or r["changed"]) else "ok"
        print(
            f"[{flag:>7}] {r['source']}: checked={r['checked']} new={r['new']} "
            f"changed={r['changed']} unchanged={r['unchanged']} baseline={r['baseline']} "
            f"gone={r['gone']} blocked={r['blocked']} errors={r['errors']} "
            f"class={r['result_class']}"
        )
        for u in r["urls"]:
            if u["status"] in ("new", "changed", "gone", "blocked", "error"):
                detail = f" ({u['detail']})" if u.get("detail") else ""
                print(f"    {u['status']:>8}  {u['url']}{detail}")

    print(f"\nSources with changes: {changed_sources or 'none'}")
    print(
        f"Dominant class: {report['dominant_class'] or 'none'} "
        f"(content_drift_detected={report['content_drift_detected']}, "
        f"check_incomplete={report['check_incomplete']})"
    )
    return 0 if args.exit_code_mode == "legacy" else int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
