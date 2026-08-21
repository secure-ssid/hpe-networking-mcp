#!/usr/bin/env python3
"""Local release validation for hpe-networking-mcp.

Runs the same practical gates used before pushing: unit tests, optional
RAG/API eval when local indexes exist, a non-mutating tool catalog count,
and an exact tool-identity freshness check when the local LanceDB tool
table exists (catches renames/swaps a count-only comparison would miss --
e.g. one tool removed and a different one added leaves the count
unchanged but the index still stale).

Strict mode (``--strict-rag --strict-tool-index``) is the release contract
and never silently skips: it additionally requires the local index manifest
pair to describe the declared RAG sources and the real artifacts
(``scripts/package_indexes.py --check-local-manifests``), the canonical
derived fact set -- including the exact ``data/specs.sqlite`` table counts --
to match ``docs/project-facts.json`` (``scripts/project_facts.py
--require-indexes``), and rejects ``--skip-rag``. A no-data CI job may
restore a pinned index bundle first; it may not run strict mode without one.

``--strict-index-facts`` is the half of that a corpus-free job can hold to:
it requires ``data/specs.sqlite`` -- rebuilt offline from the committed
``vendor/openapi`` corpus -- so the published endpoint/schema/field counts
are compared instead of skipped. Without it, a checkout that never built the
index passes the fact gate by not looking.

The tool catalog floor is not a parameter of the caller: ``--min-tools``
defaults to ``docs/project-facts.json``'s ``tools.platform_backend_total``,
so CI, the release workflow, and the docs all enforce one number.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
_BOUNDED_PREVIEW_LIMIT = 10


def _project_facts() -> ModuleType:
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from hpe_networking_mcp.pipeline import project_facts

    return project_facts


_PROJECT_FACTS = _project_facts()
_FULL_CATALOG_ENV: dict[str, str] = dict(_PROJECT_FACTS.CATALOG_ENV)
_STANDARD_CATALOG_ENV = {
    **_FULL_CATALOG_ENV,
    "HPE_MCP_GLP_GENERATED_TOOLS": "0",
}

#: The tool catalog floor, read from ``docs/project-facts.json`` rather than
#: typed here. Retyping it is how the CI gate ended up enforcing 6703 while
#: every doc promised 6711: two copies of one number drift, one cannot. It is
#: deliberately the platform API backend subtotal, not the complete registered
#: total, so the credential-free local backends can come and go without moving
#: a floor that is about vendor API coverage.
_DEFAULT_MIN_TOOLS: int = int(_PROJECT_FACTS.load()["tools"]["platform_backend_total"])


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def _rag_indexes_available(root: Path = ROOT) -> bool:
    return (root / "data/docs.lance").is_dir() and (root / "data/specs.sqlite").is_file()


def _run(command: list[str], label: str) -> None:
    print(f"\n==> {label}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _strict_env() -> dict[str, str]:
    """Environment that pins the reproducible complete-catalog selection."""
    env = os.environ.copy()
    env.update(_FULL_CATALOG_ENV)
    env.pop("HPE_MCP_PRODUCTS", None)
    return env


def _registered_tool_identities(products: str | None) -> set[str]:
    """Return exact ``"server:tool_name"`` identities for the registered catalog.

    Used both for the catalog-count floor and for the tool-index freshness
    check: comparing identities (not just a count) catches the case where
    one tool is renamed/swapped for another of the same total count -- a
    count-only comparison would call that index "fresh" when it no longer
    matches the registered catalog at all.
    """
    if products and products.strip().lower() == "all":
        env = os.environ.copy()
        env.update(_FULL_CATALOG_ENV)
        env.pop("HPE_MCP_PRODUCTS", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json\n"
                    "from scripts import ingest_tools\n"
                    "pairs = ingest_tools._collect('all')\n"
                    "ids = sorted(f'{server}:{tool[\"name\"]}' for server, tool in pairs)\n"
                    "print('__HPE_MCP_TOOL_IDS__=' + json.dumps(ids))\n"
                ),
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        marker = "__HPE_MCP_TOOL_IDS__="
        for line in reversed(completed.stdout.splitlines()):
            if line.startswith(marker):
                return set(json.loads(line.removeprefix(marker)))
        raise RuntimeError("Isolated tool catalog identities did not return an id marker")

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from scripts import ingest_tools

    previous_catalog_env = {
        name: os.environ.get(name) for name in _STANDARD_CATALOG_ENV
    }
    previous_products = os.environ.get("HPE_MCP_PRODUCTS")
    os.environ.update(_STANDARD_CATALOG_ENV)
    # products=None must mean "core servers only". _server_specs() falls back to
    # $HPE_MCP_PRODUCTS when products is None, and importing the backends
    # runs hpe_networking_mcp.mcp_servers.shared's load_dotenv() — so a developer .env enabling
    # optional starters silently turned the "core" count into the "all" count
    # (213 -> 346) and made the release gate compare docs against the wrong
    # number. Pin both vars so the count is environment-independent.
    os.environ.pop("HPE_MCP_PRODUCTS", None)
    try:
        pairs = ingest_tools._collect(products)
    finally:
        for name, previous in previous_catalog_env.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        if previous_products is None:
            os.environ.pop("HPE_MCP_PRODUCTS", None)
        else:
            os.environ["HPE_MCP_PRODUCTS"] = previous_products
    return {f"{server}:{tool['name']}" for server, tool in pairs}


def _tool_catalog_count(products: str | None) -> int:
    return len(_registered_tool_identities(products))


def _indexed_tool_identities(root: Path = ROOT) -> set[str] | None:
    """Return ``"server:tool_name"`` identities currently in the LanceDB tools table.

    Returns ``None`` when the table hasn't been built yet, matching the old
    ``_tool_index_count``'s "missing index" signal.
    """
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))
    from hpe_networking_mcp.pipeline.clients import lance_client

    db = lance_client.connect(root / "data")
    table = lance_client.tools_table(db)
    if table is None:
        return None
    rows = (
        table.search()
        .select(["server", "name"])
        .limit(table.count_rows())
        .to_arrow()
        .to_pylist()
    )
    return {f"{row['server']}:{row['name']}" for row in rows}


def _bounded_preview(identities: list[str]) -> str:
    preview = identities[:_BOUNDED_PREVIEW_LIMIT]
    suffix = "" if len(identities) <= _BOUNDED_PREVIEW_LIMIT else ", ..."
    return ", ".join(preview) + suffix


def _validate_tool_count(total: int, minimum: int) -> None:
    if total < minimum:
        raise SystemExit(f"Tool catalog count {total} is below required minimum {minimum}")


def _validate_tool_index_fresh(indexed: set[str], registered: set[str]) -> None:
    """Fail unless the indexed tool identities exactly match the registered set.

    A plain count comparison (``len(indexed) >= len(registered)``) misses
    the case where a tool was renamed or swapped for a different one: the
    count stays the same, but the index no longer reflects the current
    catalog. Comparing the exact identity sets catches that.
    """
    missing = sorted(registered - indexed)
    stale = sorted(indexed - registered)
    if not missing and not stale:
        return

    details = []
    if missing:
        details.append(
            f"{len(missing)} registered tool(s) missing from the index: "
            f"{_bounded_preview(missing)}"
        )
    if stale:
        details.append(
            f"{len(stale)} indexed tool(s) no longer registered: "
            f"{_bounded_preview(stale)}"
        )
    raise SystemExit(
        "Tool index is stale: " + "; ".join(details) + ". "
        "Rebuild with `uv run python scripts/ingest_tools.py --complete-catalog`."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true", help="do not run unit tests")
    parser.add_argument("--skip-rag", action="store_true", help="do not run the RAG/API eval gate")
    parser.add_argument(
        "--strict-rag",
        action="store_true",
        help="fail if RAG indexes are missing instead of skipping the eval gate",
    )
    parser.add_argument(
        "--catalog-products",
        default="all",
        help="optional products to include in the non-mutating catalog count",
    )
    parser.add_argument(
        "--min-tools",
        type=_positive_int,
        default=_DEFAULT_MIN_TOOLS,
        help=(
            "minimum acceptable tool catalog count; defaults to "
            f"{_DEFAULT_MIN_TOOLS}, read from docs/project-facts.json's "
            "tools.platform_backend_total. Callers should let it default so "
            "the enforced floor cannot drift from the published one."
        ),
    )
    parser.add_argument(
        "--strict-tool-index",
        action="store_true",
        help="fail if the local LanceDB tools index is missing",
    )
    parser.add_argument(
        "--strict-index-facts",
        action="store_true",
        help=(
            "require data/specs.sqlite so the offline-derivable OpenAPI counts "
            "are compared rather than silently skipped (implied by --strict-rag; "
            "does not require the scraped prose corpus)"
        ),
    )
    parser.add_argument(
        "--ignore-index-facts",
        action="store_true",
        help=(
            "compare code-derived project facts only; skip indexes.* even when "
            "data/ exists (CI against a pinned older docs/API bundle)"
        ),
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    if args.strict_rag and args.skip_rag:
        # --skip-rag used to win silently, so `--strict-rag --skip-rag`
        # reported success without ever running the eval. Strict mode must
        # fail loudly instead of quietly dropping its own gate.
        raise SystemExit("--strict-rag cannot be combined with --skip-rag")

    if args.strict_index_facts and args.ignore_index_facts:
        # One demands the offline index facts be compared, the other drops
        # index facts entirely. Silently letting either win is how a gate
        # ends up reporting success over a comparison it never made.
        raise SystemExit("--strict-index-facts cannot be combined with --ignore-index-facts")

    if not args.skip_tests:
        _run([sys.executable, "-m", "pytest", "tests/unit", "-q"], "Unit tests")

    if not args.skip_rag:
        if _rag_indexes_available():
            _run(
                [sys.executable, "tests/eval/run_eval.py", "--ci"],
                "RAG/API eval gate",
            )
        elif args.strict_rag:
            raise SystemExit("RAG indexes missing: expected data/docs.lance and data/specs.sqlite")
        else:
            print("\n==> RAG/API eval gate", flush=True)
            print("Skipping: data/docs.lance or data/specs.sqlite is missing.", flush=True)

    _run(
        [sys.executable, "scripts/check_generated_tool_manifests.py"],
        "Generated tool manifests",
    )
    _run(
        [sys.executable, "scripts/report_capability_gaps.py", "--check"],
        "Capability gap report",
    )

    # The declared RAG source list itself: a source whose scraper path or
    # ingest_docs SOURCE_META entry disappeared would otherwise only surface
    # as a silently missing corpus at the next rebuild.
    _run(
        [sys.executable, "scripts/validate_source_manifest.py"],
        "Declared RAG source manifest",
    )

    # Local index manifest pair: catches the stale downloaded
    # data/SOURCE-MANIFEST.json (9 sources) sitting beside a generated
    # data/INDEX-MANIFEST.json (16 sources), and any manifest that no longer
    # describes the artifacts actually on disk.
    #
    # Demanding every artifact is a --strict-rag concern, not a
    # --strict-tool-index one. The tool catalog rebuilds from the committed
    # server modules, but data/specs.sqlite is built from ingestion/sources/,
    # which is gitignored, and data/docs.lance is the scraped prose corpus this
    # project does not distribute. Requiring them here made the tool-index gate
    # impossible to satisfy on a clean checkout.
    manifest_command = [sys.executable, "scripts/package_indexes.py", "--check-local-manifests"]
    if not args.strict_rag:
        manifest_command.append("--allow-missing-artifacts")
    _run(manifest_command, "Local index manifests")

    # Canonical derived facts (package version, server IDs, per-backend tool
    # counts, generated-operation counts, exact specs.sqlite table counts,
    # RAG artifact/source counts). --require-indexes makes the exact SQLite
    # counts part of the contract rather than an optional extra that vanishes
    # with the data directory: without it, a checkout that never built
    # data/specs.sqlite skips indexes.offline_derivable.* entirely and still
    # reports success, so the published 2,734/6,363/31,432 OpenAPI counts are
    # never compared against anything.
    #
    # --require-indexes demands only the offline-derivable section, which any
    # clone rebuilds from the committed vendor/openapi corpus in seconds, so
    # --strict-index-facts is a gate a no-corpus CI job can actually pass.
    # --strict-rag implies it: a run that requires the scraped corpus requires
    # the index built beside it. Passing --ignore-index-facts alongside either
    # used to emit a self-contradicting `--require-indexes --ignore-indexes`,
    # where the requirement won and the caller's opt-out was silently ignored.
    facts_command = [sys.executable, "scripts/project_facts.py"]
    if args.ignore_index_facts:
        facts_command.append("--ignore-indexes")
    elif args.strict_rag or args.strict_index_facts:
        facts_command.append("--require-indexes")
    print("\n==> Canonical project facts", flush=True)
    subprocess.run(facts_command, cwd=ROOT, check=True, env=_strict_env())

    print("\n==> Tool catalog count", flush=True)
    registered_ids = _registered_tool_identities(args.catalog_products)
    total = len(registered_ids)
    print(f"{total} tools discovered with products={args.catalog_products!r}")
    _validate_tool_count(total, args.min_tools)
    print(f"Tool catalog floor satisfied: {total} >= {args.min_tools}")

    indexed_ids = _indexed_tool_identities()
    if indexed_ids is None:
        if args.strict_tool_index:
            raise SystemExit("Tool index missing: expected a LanceDB tools table under data/")
        print("Tool index freshness skipped: local LanceDB tools table is missing.")
    else:
        print(f"Tool index contains {len(indexed_ids)} tools")
        _validate_tool_index_fresh(indexed_ids, registered_ids)
        print(
            f"Tool index freshness satisfied: {len(indexed_ids)} indexed tools "
            "exactly match the registered catalog"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
