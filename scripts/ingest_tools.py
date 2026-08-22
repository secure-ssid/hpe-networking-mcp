"""Embed MCPServer tool definitions into the router tools index.

Usage: uv run python scripts/ingest_tools.py                  # LanceDB (embedded, default)
       uv run python scripts/ingest_tools.py --backend redis  # optional Redis Stack
       uv run python scripts/ingest_tools.py --products clearpass,mist
       uv run python scripts/ingest_tools.py --complete-catalog

For the complete catalog that `scripts/validate_release.py --strict-tool-index`
compares against, use ``--complete-catalog``. It applies the same canonical,
fully pinned environment as the release gates and includes every optional
product without inheriting stale product or generated-tool settings.

Reconcile data/INDEX-MANIFEST.json and docs/project-facts.json afterwards
(`scripts/package_indexes.py --write-local-manifests`,
`scripts/project_facts.py --write`), or the manifest/facts gates will flag the
rebuilt index.

Embedding runs on a pool of data-parallel ONNX sessions. Pool size defaults
to ``min(cpu_count, 8)`` — capped because each session loads its own model
copy and an uncapped default exhausts memory on many-core hosts. Set
``HPE_MCP_INGEST_PARALLEL`` to override in either direction.

Reads the servers by direct module import (no subprocess) and walks the
`mcp._tool_manager._tools` registry. Each tool becomes one indexed row with:
  payload: {server, name, description, schema_json}
  vector:  embedding of "name\\ndescription\\nparam_names"
"""
import argparse
import hashlib
import importlib
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

SERVERS = [
    ("central-config", "hpe_networking_mcp.mcp_servers.config"),
    ("central-monitoring", "hpe_networking_mcp.mcp_servers.monitoring"),
    ("central-nac", "hpe_networking_mcp.mcp_servers.nac"),
    ("central-ops", "hpe_networking_mcp.mcp_servers.ops"),
    ("central-streaming", "hpe_networking_mcp.mcp_servers.central_streaming"),
    ("site-health", "hpe_networking_mcp.mcp_servers.site_health"),
    ("glp-core", "hpe_networking_mcp.mcp_servers.glp"),
    ("rag-core", "hpe_networking_mcp.mcp_servers.rag"),
    # Credential-free and always loaded by the router (tool_router's
    # _ALWAYS_ON_BACKENDS), so it is always indexed too.
    ("interop-core", "hpe_networking_mcp.mcp_servers.interop"),
]
OPTIONAL_SERVERS = {
    "central-generated": ("central-generated", "hpe_networking_mcp.mcp_servers.central_generated"),
    "clearpass": ("clearpass-core", "hpe_networking_mcp.mcp_servers.clearpass"),
    "mist": ("mist-core", "hpe_networking_mcp.mcp_servers.mist"),
    "apstra": ("apstra-core", "hpe_networking_mcp.mcp_servers.apstra"),
    "aos8": ("aos8-core", "hpe_networking_mcp.mcp_servers.aos8"),
    "edgeconnect": ("edgeconnect-core", "hpe_networking_mcp.mcp_servers.edgeconnect"),
    "uxi": ("uxi-core", "hpe_networking_mcp.mcp_servers.uxi"),
    "axis": ("axis-core", "hpe_networking_mcp.mcp_servers.axis"),
    "design": ("design-core", "hpe_networking_mcp.mcp_servers.design"),
}


def _csv(value: str | None) -> list[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def _product_access() -> str:
    from hpe_networking_mcp.mcp_servers.shared import optional_product_access_mode

    return optional_product_access_mode()


def _configure_complete_catalog() -> str:
    from hpe_networking_mcp.pipeline import project_facts

    os.environ.update(project_facts.CATALOG_ENV)
    os.environ.pop("HPE_MCP_PRODUCTS", None)
    return "all"


def _is_read_only_tool(tool) -> bool:
    # MCP SDK v2 ToolAnnotations use snake_case (read_only_hint).
    annotations = getattr(tool, "annotations", None)
    return bool(
        getattr(annotations, "read_only_hint", None)
        if annotations is not None
        else False
    )


def _server_specs(products: str | None = None) -> list[tuple[str, str]]:
    """Return core server specs plus optional products requested by arg/env."""
    requested = _csv(products if products is not None else os.getenv("HPE_MCP_PRODUCTS"))
    specs = list(SERVERS)
    if "all" in requested:
        requested = list(OPTIONAL_SERVERS)
    for product in requested:
        spec = OPTIONAL_SERVERS.get(product)
        if spec and spec not in specs:
            specs.append(spec)
    return specs


def _stable_id(server: str, tool: str) -> str:
    h = hashlib.sha1(f"{server}:{tool}".encode()).hexdigest()
    return str(uuid.UUID(h[:32]))


def _extract_tools(module_path: str, *, include_writes: bool = True) -> list[dict]:
    mod = importlib.import_module(module_path)
    manager = mod.mcp._tool_manager
    out = []
    for name, tool in manager._tools.items():
        if not include_writes and not _is_read_only_tool(tool):
            continue
        schema = tool.parameters if isinstance(tool.parameters, dict) else {}
        params = list((schema.get("properties") or {}).keys())
        out.append({
            "name": name,
            "description": (tool.description or "").strip(),
            "schema": schema,
            "params": params,
        })
    return out


def _embed_text(t: dict) -> str:
    # Repeat the name — tool names carry most of the semantic signal but are
    # short relative to docstrings; duplication lifts name-match recall.
    name_words = t["name"].replace("_", " ")
    return (
        f"{t['name']}\n{name_words}\n{name_words}\n"
        f"{t['description']}\nparams: {', '.join(t['params'])}"
    )


def _collect(products: str | None = None) -> list[tuple[str, dict]]:
    out = []
    include_optional_writes = _product_access() == "read-write"
    optional_server_names = {server for server, _ in OPTIONAL_SERVERS.values()}
    for server, module_path in _server_specs(products):
        tools = _extract_tools(
            module_path,
            include_writes=include_optional_writes or server not in optional_server_names,
        )
        print(f"  {server}: {len(tools)} tools")
        out.extend((server, t) for t in tools)
    return out


def main_lancedb(products: str | None = None) -> int:
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    pairs = _collect(products)
    embedder = EmbedClient()
    # Data-parallel ONNX sessions in one pool, spawned once (see
    # iter_embed_documents). fastembed's ordered_map re-keys results by
    # input index, so output order is preserved and the zip below stays
    # correct. Worker spawning needs this script's __main__ guard (present).
    # Default is capped at 8: each session loads its own model copy, and an
    # uncapped cpu_count default OOMs many-core hosts (onnxruntime
    # bad_alloc). HPE_MCP_INGEST_PARALLEL overrides in both directions.
    parallel = int(
        os.getenv("HPE_MCP_INGEST_PARALLEL") or min(os.cpu_count() or 1, 8)
    )
    vectors = list(
        embedder.iter_embed_documents(
            [_embed_text(t) for _, t in pairs], parallel=parallel
        )
    )
    rows = [
        {
            "id": _stable_id(server, t["name"]),
            "server": server,
            "name": t["name"],
            "description": t["description"],
            "schema_json": json.dumps(t["schema"]),
            # FTS half of hybrid tool search runs over this column
            "fts_text": (f"{t['name'].replace('_', ' ')} {t['name']} "
                         f"{t['description']} {' '.join(t['params'])}"),
            "vector": vec,
        }
        for (server, t), vec in zip(pairs, vectors, strict=True)
    ]
    db = lance_client.connect()
    lance_client.create_tools_table(db, rows)
    print(f"Ingested {len(rows)} tools into LanceDB '{lance_client.TOOLS_TABLE}'")
    return 0


def main_redis(products: str | None = None) -> int:
    from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient
    from hpe_networking_mcp.pipeline.clients.redis_client import (
        TOOLS_INDEX,
        ensure_tools_index,
        get_client,
        upsert_tools,
    )

    ollama = OllamaClient()
    client = get_client()

    # Drop and recreate the index for a clean re-ingest
    try:
        client.ft(TOOLS_INDEX).dropindex(delete_documents=True)
        print(f"Dropped existing index '{TOOLS_INDEX}'")
    except Exception:
        pass
    ensure_tools_index(client)

    batch: list[dict] = []
    for server, t in _collect(products):
        vec = ollama.embed(_embed_text(t))
        batch.append({
            "id": _stable_id(server, t["name"]),
            "server": server,
            "name": t["name"],
            "description": t["description"],
            "schema_json": json.dumps(t["schema"]),
            "params": t["params"],
            "embedding": vec,
        })

    upsert_tools(client, batch)
    print(f"Ingested {len(batch)} tools into '{TOOLS_INDEX}'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("lancedb", "redis"), default="lancedb")
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument(
        "--products",
        default=None,
        help=(
            "Optional product catalog entries to include, comma-separated "
            "(central-generated,clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design,all). "
            "Defaults to HPE_MCP_PRODUCTS."
        ),
    )
    selection.add_argument(
        "--complete-catalog",
        action="store_true",
        help="pin the canonical full release environment and include all products",
    )
    args = ap.parse_args()
    products = _configure_complete_catalog() if args.complete_catalog else args.products
    return main_redis(products) if args.backend == "redis" else main_lancedb(products)


if __name__ == "__main__":
    sys.exit(main())
