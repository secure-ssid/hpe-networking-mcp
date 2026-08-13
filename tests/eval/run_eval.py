#!/usr/bin/env python3
"""RAG evaluation runner for hpe-networking-mcp.

Measures whether retrieval selects the *correct* info, so a backend/retrieval
change (e.g. Redis -> LanceDB, adding hybrid+rerank, nomic prefixes) can be
proven instead of asserted. See docs/architecture/RAG-ARCHITECTURE.md.

Usage:
    uv run python tests/eval/run_eval.py                 # default top_k=5
    uv run python tests/eval/run_eval.py --k 8 --verbose
    uv run python tests/eval/run_eval.py --json out.json # machine-readable
    uv run python tests/eval/run_eval.py --ci            # enforce quality thresholds

Metrics:
    source_hit@k  - an expected source substring appears in a top-k file_path/source
    keyword_hit   - an expected keyword appears in returned text (case-insensitive)
    mrr           - reciprocal rank of the first source hit (0 if none)
    api_exact     - for api-lookup rows, keyword_hit treated as the exact-answer signal
                    (ideally served by a future lookup_api tool; falls back to search_docs)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: `uv run --with pyyaml python tests/eval/run_eval.py`")


def _resolve(modattr):
    """Return a plain callable for a (possibly MCPServer-wrapped) tool, or None."""
    mod_name, attr = modattr
    try:
        import importlib
        mod = importlib.import_module(mod_name)
    except Exception:
        return None
    obj = getattr(mod, attr, None)
    if obj is None:
        return None
    # MCPServer may wrap the function; the raw callable is usually at .fn
    return getattr(obj, "fn", obj)


def load_questions() -> list[dict]:
    data = yaml.safe_load((Path(__file__).parent / "rag_eval.yaml").read_text())
    return data["questions"]


def run(k: int, verbose: bool) -> dict:
    search_docs = _resolve(("hpe_networking_mcp.mcp_servers.rag", "search_docs"))
    lookup_api = _resolve(("hpe_networking_mcp.mcp_servers.rag", "lookup_api"))
    lookup_advisory = _resolve(("hpe_networking_mcp.mcp_servers.rag", "lookup_advisory"))
    check_product_lifecycle = _resolve(
        ("hpe_networking_mcp.mcp_servers.rag", "check_product_lifecycle")
    )
    list_advisories = _resolve(("hpe_networking_mcp.mcp_servers.rag", "list_advisories"))
    list_lifecycle_events = _resolve(("hpe_networking_mcp.mcp_servers.rag", "list_lifecycle_events"))
    correlate_advisory_lifecycle = _resolve(
        ("hpe_networking_mcp.mcp_servers.rag", "correlate_advisory_lifecycle")
    )
    rag_diagnostics = _resolve(("hpe_networking_mcp.mcp_servers.rag", "rag_diagnostics"))
    if search_docs is None:
        sys.exit("Could not import hpe_networking_mcp.mcp_servers.rag.search_docs — is the backend reachable?")

    # Bounded, dict-shaped structured tools (list/correlate/diagnostics) are
    # scored by treating the *whole* returned dict as one "hit" — the
    # generic blob-matching below then scans its full JSON, including
    # nested pagination/correlation fields, rather than requiring a
    # top-level source/file_path key.
    _DICT_TOOLS = {
        "list-advisories": list_advisories,
        "list-lifecycle": list_lifecycle_events,
        "correlate": correlate_advisory_lifecycle,
        "diagnostics": rag_diagnostics,
    }

    questions = load_questions()
    rows = []
    for q in questions:
        # Prefer exact structured lookup for api-lookup once lookup_api exists.
        results = None
        if q["type"] == "api-lookup" and lookup_api is not None:
            try:
                results = lookup_api(q["query"])
            except Exception:
                results = None
            # Empty or error-only -> specs hold no confident answer; fall back
            # to prose search (mirrors how an agent uses the two tools).
            if not results or all("error" in h for h in results if isinstance(h, dict)):
                results = None
        elif q["type"] == "advisory" and lookup_advisory is not None:
            try:
                results = lookup_advisory(**q["arguments"])
            except Exception:
                results = None
        elif q["type"] == "lifecycle" and check_product_lifecycle is not None:
            try:
                results = check_product_lifecycle(**q["arguments"])
            except Exception:
                results = None
        elif q["type"] in _DICT_TOOLS and _DICT_TOOLS[q["type"]] is not None:
            try:
                results = [_DICT_TOOLS[q["type"]](**q.get("arguments", {}))]
            except Exception as e:
                rows.append({**_blank(q), "error": str(e)})
                continue
        if results is None:
            try:
                results = search_docs(q["query"], top_k=k)
            except Exception as e:
                rows.append({**_blank(q), "error": str(e)})
                continue

        hits = results if isinstance(results, list) else [results]

        # A row tagged expect_empty asserts the tool correctly found
        # *nothing* in these official sources (a fabricated/non-official
        # non-empty answer would be worse than an honest empty one) — e.g.
        # the documented current-Aruba-lifecycle coverage gap, or a
        # deliberately bogus CVE/SKU. Score it on emptiness, not keywords.
        if q.get("expect_empty"):
            empty_ok = len(hits) == 0
            rows.append({
                "id": q["id"], "type": q["type"],
                "source_hit": empty_ok, "rank": 1 if empty_ok else 0,
                "keyword_hit": empty_ok,
                "mrr": 1.0 if empty_ok else 0.0,
            })
            if verbose:
                print(f"  {q['id']:<28} expect_empty={empty_ok!s}")
            continue

        # source_hit + mrr — matched against each hit's full JSON dump (not
        # just source/source_family/file_path) so structured list/correlate/
        # diagnostics results can be matched on any nested field.
        src_rank = 0
        for i, h in enumerate(hits[:k], start=1):
            blob = json.dumps(h, sort_keys=True, default=str).lower()
            if any(s.lower() in blob for s in q.get("expect_sources", [])):
                src_rank = i
                break
        # keyword_hit across all returned text
        text_all = " ".join(
            json.dumps(h, sort_keys=True, default=str) for h in hits
        ).lower()
        kw_hit = any(kw.lower() in text_all for kw in q.get("expect_keywords", []))

        rows.append({
            "id": q["id"], "type": q["type"],
            "source_hit": src_rank > 0, "rank": src_rank,
            "keyword_hit": kw_hit,
            "mrr": (1.0 / src_rank) if src_rank else 0.0,
        })
        if verbose:
            print(f"  {q['id']:<28} src_hit={rows[-1]['source_hit']!s:<5} "
                  f"rank={src_rank} kw={kw_hit}")

    return _aggregate(rows)


def _blank(q):
    return {"id": q["id"], "type": q["type"], "source_hit": False,
            "rank": 0, "keyword_hit": False, "mrr": 0.0}


# New v0.7 structured tool types (bounded list/correlate/diagnostics) — kept
# separate from "structured_exact" (advisory/lifecycle lookup) so a stale
# baseline expectation for the original two types is never silently diluted
# by averaging in unrelated new types.
_NEW_STRUCTURED_TYPES = ("list-advisories", "list-lifecycle", "correlate", "diagnostics")


def _aggregate(rows: list[dict]) -> dict:
    def frac(pred, subset=None):
        rs = [r for r in rows if (subset is None or r["type"] == subset)]
        return (sum(1 for r in rs if pred(r)) / len(rs)) if rs else 0.0

    present_new = [t for t in _NEW_STRUCTURED_TYPES if any(r["type"] == t for r in rows)]
    structured_list_exact = (
        round(
            sum(frac(lambda r: r["source_hit"] and r["keyword_hit"], t) for t in present_new)
            / len(present_new),
            3,
        )
        if present_new
        else 0.0
    )

    summary = {
        "n": len(rows),
        "source_hit@k": round(frac(lambda r: r["source_hit"]), 3),
        "keyword_hit": round(frac(lambda r: r["keyword_hit"]), 3),
        "mrr": round(sum(r["mrr"] for r in rows) / len(rows), 3) if rows else 0.0,
        "howto_recall@k": round(frac(lambda r: r["source_hit"], "howto"), 3),
        "api_exact": round(frac(lambda r: r["keyword_hit"], "api-lookup"), 3),
        "structured_exact": round(
            frac(
                lambda r: r["source_hit"] and r["keyword_hit"],
                "advisory",
            )
            + frac(
                lambda r: r["source_hit"] and r["keyword_hit"],
                "lifecycle",
            ),
            3,
        )
        / 2,
        "structured_list_exact": structured_list_exact,
        "rows": rows,
    }
    return summary


_DEFAULT_THRESHOLDS = {
    "source_hit@k": 0.85,
    "mrr": 0.85,
    "howto_recall@k": 0.85,
    "api_exact": 0.95,
    "structured_exact": 1.0,
    "structured_list_exact": 1.0,
}
# NOTE: the bars sit just under the current measured scores on the 33-question
# set (source_hit@k 0.97, mrr 0.923, howto_recall@k 1.0, api_exact 1.0,
# structured_exact 1.0, structured_list_exact 1.0) so a retrieval regression
# fails the gate while normal run-to-run jitter does not. They were raised to
# 0.85 once tech_docs/vsg_docs/nac_docs were fully scraped and indexed; the
# earlier lower bars existed only while those sources were unavailable.
# See docs/architecture/RAG-ARCHITECTURE.md and
# ingestion/source_manifest.json for the tracked source list.


def _threshold_failures(summary: dict, thresholds: dict[str, float]) -> list[str]:
    failures = []
    for metric, minimum in thresholds.items():
        actual = float(summary.get(metric, 0.0))
        if actual < minimum:
            failures.append(f"{metric}={actual:.3f} < {minimum:.3f}")
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", help="write full results to this path")
    ap.add_argument("--ci", action="store_true", help="enforce default eval thresholds")
    ap.add_argument("--min-source-hit", type=float, default=None)
    ap.add_argument("--min-mrr", type=float, default=None)
    ap.add_argument("--min-howto-recall", type=float, default=None)
    ap.add_argument("--min-api-exact", type=float, default=None)
    ap.add_argument("--min-structured-exact", type=float, default=None)
    ap.add_argument("--min-structured-list-exact", type=float, default=None)
    args = ap.parse_args()

    print(f"Running RAG eval (top_k={args.k})...")
    summary = run(args.k, args.verbose)
    print("\n=== RAG eval summary ===")
    for key in (
        "n",
        "source_hit@k",
        "keyword_hit",
        "mrr",
        "howto_recall@k",
        "api_exact",
        "structured_exact",
        "structured_list_exact",
    ):
        print(f"  {key:<16} {summary[key]}")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2))
        print(f"\nWrote {args.json}")

    # Non-zero exit if nothing retrieved at all (sanity gate for CI).
    if summary["source_hit@k"] == 0 and summary["keyword_hit"] == 0:
        sys.exit("FAIL: zero retrieval signal — backend likely unreachable or empty index.")

    thresholds: dict[str, float] = {}
    if args.ci:
        thresholds.update(_DEFAULT_THRESHOLDS)
    explicit = {
        "source_hit@k": args.min_source_hit,
        "mrr": args.min_mrr,
        "howto_recall@k": args.min_howto_recall,
        "api_exact": args.min_api_exact,
        "structured_exact": args.min_structured_exact,
        "structured_list_exact": args.min_structured_list_exact,
    }
    thresholds.update({metric: value for metric, value in explicit.items() if value is not None})
    if thresholds:
        failures = _threshold_failures(summary, thresholds)
        if failures:
            sys.exit("FAIL: eval thresholds not met: " + "; ".join(failures))


if __name__ == "__main__":
    main()
