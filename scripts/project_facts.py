#!/usr/bin/env python3
"""Generate or drift-check docs/project-facts.json, the canonical fact set.

``docs/project-facts.json`` is the one machine-readable place every doc,
release note, and gate reads counts from: package version, MCP server IDs,
per-backend registered tool counts, generated-operation counts, the exact
structured API SQLite counts, and RAG artifact/source counts. It is derived,
never hand-written -- see
``src/hpe_networking_mcp/pipeline/project_facts.py``.

Usage:
    uv run python scripts/project_facts.py                      # check for drift
    uv run python scripts/project_facts.py --write              # regenerate
    uv run python scripts/project_facts.py --require-indexes    # strict: indexes must exist
    uv run python scripts/project_facts.py --skip-router-modes
        # fast: skip the ~15s router-mode probe
    uv run python scripts/project_facts.py --print              # dump derived facts

The complete tool catalog is only reproducible with every guarded write and
generated operation registered, so this script applies the canonical
``project_facts.CATALOG_ENV`` before importing any backend and clears
``HPE_MCP_PRODUCTS`` so a developer ``.env`` cannot change the answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import project_facts  # noqa: E402

# Pinned before importing the backends: hpe_networking_mcp.mcp_servers.shared
# runs load_dotenv() at import time, and generated backends read their feature
# flags while registering, so a late override would silently change counts.
os.environ.update(project_facts.CATALOG_ENV)
os.environ.pop("HPE_MCP_PRODUCTS", None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate docs/project-facts.json from the current checkout",
    )
    parser.add_argument(
        "--print",
        dest="print_facts",
        action="store_true",
        help="print the derived facts as JSON instead of comparing",
    )
    parser.add_argument(
        "--require-indexes",
        action="store_true",
        help="fail when the local data/ indexes are missing instead of skipping index facts",
    )
    parser.add_argument(
        "--ignore-indexes",
        action="store_true",
        help=(
            "do not compare indexes.* facts even when data/ is present "
            "(CI against a pinned older index bundle)"
        ),
    )
    parser.add_argument(
        "--skip-router-modes",
        action="store_true",
        help=(
            "skip the minimal/default/direct-all router tool count probe "
            "(saves ~15s of subprocess imports; --write preserves the previously "
            "tracked router_modes facts instead of erasing them)"
        ),
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    current = project_facts.collect(include_router_modes=not args.skip_router_modes)

    if args.print_facts:
        print(json.dumps(current, indent=2))
        return 0

    if args.write:
        if args.require_indexes and current.get("indexes") is None:
            print(
                "Refusing to write index-free facts with --require-indexes: "
                "data/specs.sqlite and data/docs.lance are missing.",
                file=sys.stderr,
            )
            return 1
        try:
            tracked = project_facts.load()
        except project_facts.ProjectFactsError:
            tracked = {}
        # Preserve previously recorded locally-built index facts rather than
        # erasing them: the scraped artifacts are git-ignored, so "not present
        # here" is not evidence that they changed. Offline-derivable counts are
        # always taken fresh -- any clone can rebuild those.
        current["indexes"] = project_facts.merge_unbuilt_index_facts(
            current.get("indexes"), tracked.get("indexes")
        )
        if current.get("router_modes") is None:
            try:
                tracked = project_facts.load()
            except project_facts.ProjectFactsError:
                tracked = {}
            # Same reasoning as indexes above: --skip-router-modes means "I
            # didn't measure it this run", not "it changed to nothing".
            current["router_modes"] = tracked.get("router_modes")
        path = project_facts.write(current)
        print(f"Wrote {path.relative_to(project_facts.REPO_ROOT)}")
        return 0

    try:
        tracked = project_facts.load()
    except project_facts.ProjectFactsError as exc:
        print(f"Canonical project facts: {exc}", file=sys.stderr)
        return 1

    problems = project_facts.compare(
        tracked,
        current,
        require_indexes=args.require_indexes,
        ignore_indexes=args.ignore_indexes,
    )
    if problems:
        print("Canonical project facts are stale:")
        for problem in problems:
            print(f"  - {problem}")
        print("Regenerate with `uv run python scripts/project_facts.py --write`.")
        return 1

    tools = current["tools"]
    indexes = current.get("indexes") or {}
    local = indexes.get(project_facts.LOCALLY_BUILT) or {}
    unbuilt = project_facts.unbuilt_index_families(current)
    router_modes = current.get("router_modes") or {}
    router_tools = router_modes.get("tools") or {}
    print(
        f"Canonical project facts match: {current['package']['name']} "
        f"{current['package']['version']}, {len(current['servers']['server_ids'])} MCP servers, "
        f"{tools['registered_total']} registered tools "
        f"({tools['platform_backend_total']} platform backend, "
        f"{tools['generated_registered']} generated, {tools['curated_total']} curated), "
        f"{current['generated_operations']['total']} generated operations, "
        f"{current['rag_sources']['count']} declared RAG sources"
        + (
            f", {local.get('docs_lance', {}).get('rows', 0)} doc chunks"
            if local.get("docs_lance")
            else ""
        )
        + (
            f", router modes minimal={router_tools.get('minimal')}/"
            f"default={router_tools.get('default')}/direct-all={router_tools.get('direct_all')}"
            if router_tools
            else " (router-mode facts skipped: --skip-router-modes)"
        )
    )
    # Not built is not drift. Say which artifacts were simply absent, so a
    # clean run on a fresh clone cannot be misread as having checked them.
    if unbuilt:
        print(
            "Locally-built index facts not compared (artifact absent, not drift): "
            + ", ".join(unbuilt)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
