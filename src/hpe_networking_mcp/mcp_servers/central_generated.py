"""MCP server — generated Aruba Central operations (central-generated).

This backend exposes *every* unique operation in the committed merged Central
manifest (``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json``) as a directly
callable, typed MCPServer tool. The manifest is built from the reviewed Aruba
ReadMe OpenAPI registry snapshot: hundreds of network-config specs plus the
Monitoring, Notifications, Reporting, Services, and Troubleshooting
registries. Each spec's server base path is folded onto its operation paths so
identical operations across sibling specs are deduplicated to one tool.

Design notes:

* This is a **separate** MCPServer server from the curated ``central-config`` /
  ``central-monitoring`` / ``central-nac`` / ``central-ops`` backends, so the ~1.7k
  generated ``central_*`` tools never collide with (or replace) the curated,
  hand-tuned Central tools.
* Reads (GET/HEAD) execute directly and are response-bounded.
* Writes (POST/PUT/PATCH/DELETE) run behind the Central write gate
  (``HPE_MCP_CENTRAL_WRITES``, denied by default) and every generated write
  defaults to ``dry_run=True`` and requires ``confirm=True`` to execute.
* JSON, ``application/x-www-form-urlencoded``, ``multipart/form-data`` and raw
  bodies are all supported, chosen from the manifest's declared content type.
* Trusted auth is injected *last* by reusing the source-account
  :class:`CentralClient` token pattern; auth header/cookie params are never
  model-visible (the shared runtime strips them from tool signatures).

Registration is guarded by ``HPE_MCP_CENTRAL_GENERATED_TOOLS`` and defaults
ON when the committed manifest exists.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import make_read_executor, make_write_executor
from hpe_networking_mcp.mcp_servers.shared import (
    get_client,
    platform_write_blocked,
    platform_writes_allowed,
)

mcp = MCPServer("central-generated")

# Populated at registration time from the committed manifest; used as a
# defense-in-depth allow-list so a generated tool can only ever hit a path
# family that the manifest actually declares. The shared runtime already
# URL-escapes path values and rejects traversal, so this is belt-and-suspenders.
_CENTRAL_ALLOWED_PREFIXES: tuple[str, ...] = ("/network-config/",)

_EXECUTE_HINT = (
    "Re-run with dry_run=False and confirm=True to execute this Central write "
    "(requires HPE_MCP_ACCESS_PROFILE=full-read-write or "
    "HPE_MCP_ACCESS_PROFILE=custom with HPE_MCP_CENTRAL_WRITES=1)."
)

# Auth header/cookie names that must never be forwarded from model-supplied
# header params — trusted auth is injected last by _central_auth_headers.
_AUTH_HEADER_NAMES = {"authorization", "cookie"}


def _central_allowed_prefixes() -> tuple[str, ...]:
    return _CENTRAL_ALLOWED_PREFIXES


def _central_path_ok(path: str) -> bool:
    return path.startswith(_CENTRAL_ALLOWED_PREFIXES)


async def _central_auth_headers(
    path: str | dict[str, str] | None,
    extra: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return ``(base_url, headers)`` with trusted Central auth injected last.

    Reuses the source-account CentralClient's token manager for the OAuth
    bearer token (the same credential the curated tools use) and injects the
    Authorization header last. Non-auth header params from the model are
    preserved; any credential-bearing header is dropped before auth is applied.
    The CentralClient's httpx session is never touched here.
    """
    if not isinstance(path, str):
        extra = path
    client = get_client()
    # Acquire/rotate the bearer token off the event loop via the same token
    # manager the CentralClient uses; never touch the client's httpx session
    # (that boundary is owned exclusively by CentralClient).
    token = await asyncio.to_thread(client.token_manager.get_access_token)
    headers: dict[str, str] = {"Accept": "application/json"}
    for key, value in (extra or {}).items():
        if key.strip().lower() in _AUTH_HEADER_NAMES:
            continue
        headers[key] = str(value)
    headers["Authorization"] = f"Bearer {token}"  # trusted auth injected last
    return client.base_url, headers


async def _central_refresh_auth() -> None:
    client = get_client()
    await asyncio.to_thread(client.token_manager.get_access_token, True)


_central_generated_read = make_read_executor(
    resolve=_central_auth_headers,
    allowed_prefixes=_central_allowed_prefixes,
    not_configured="Central not configured",
    refresh_auth=_central_refresh_auth,
)

_central_generated_write = make_write_executor(
    resolve=_central_auth_headers,
    allowed_prefixes=_central_allowed_prefixes,
    writes_allowed=lambda: platform_writes_allowed("central"),
    blocked_response=lambda name: platform_write_blocked("central", name),
    execute_hint=_EXECUTE_HINT,
    not_configured="Central not configured",
    refresh_auth=_central_refresh_auth,
)


def _register_generated_central_tools() -> list[str]:
    """Register generated Central tools at import time.

    No-op (returns ``[]``) when the feature flag is off or the manifest is
    missing, so a stripped checkout never breaks import.
    """
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest, manifest_exists
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import (
        generated_tools_enabled,
        register_generated_tools,
    )

    if not generated_tools_enabled("central") or not manifest_exists("central"):
        return []
    manifest = load_manifest("central")
    global _CENTRAL_ALLOWED_PREFIXES
    prefixes = sorted(
        {
            "/" + op["path"].split("/", 2)[1] + "/"
            for op in manifest.get("operations", [])
            if isinstance(op.get("path"), str) and op["path"].startswith("/")
        }
    )
    if prefixes:
        _CENTRAL_ALLOWED_PREFIXES = tuple(prefixes)
    return register_generated_tools(
        mcp,
        "central",
        read_executor=_central_generated_read,
        write_executor=_central_generated_write,
        manifest=manifest,
    )


GENERATED_CENTRAL_TOOLS = _register_generated_central_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )
    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    run_server(mcp)
