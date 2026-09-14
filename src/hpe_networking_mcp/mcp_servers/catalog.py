"""Always-available local hardware SKU catalog MCP backend.

This backend is intentionally independent of ``rag-core``: it reads the
locally built SQLite catalog directly and never imports LanceDB, an embedder,
or vendor documentation.  It is therefore available to every router profile.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.shared import READ_ONLY_LOCAL
from hpe_networking_mcp.pipeline.clients import hardware_catalog

mcp = MCPServer("catalog-core")


@mcp.tool(annotations=READ_ONLY_LOCAL)
def search_hardware_catalog(
    query: str,
    vendor: str | None = None,
    include_specs: bool = False,
    include_taa: bool = False,
    multigig_only: bool = False,
    wifi_standard: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Find HPE Aruba or HPE Juniper hardware SKUs without RAG.

    Searches a local SQLite catalog: exact SKU/part-number aliases first, then
    deterministic model/configuration candidates. Use a phrase such as
    ``CX 6300 PoE 48 port`` when the exact SKU is not known. Responses carry
    concise identity data and official source links; detailed specs are opt-in.

    TAA variants are federal-procurement duplicates of the standard SKUs and
    are withheld by default so family listings stay readable; the response
    reports how many were withheld. Raise ``limit`` when the user asks for a
    whole family: ``total_matches`` reports how many matched.

    Args:
        query: SKU, model, or configuration requirement.
        vendor: Optional ``aruba`` or ``juniper`` filter.
        include_specs: Include detailed normalized specifications (default false).
        include_taa: Include TAA federal-procurement variants (default false).
        multigig_only: Only Smart Rate / multi-gigabit models (default false).
        wifi_standard: Filter access points by generation, e.g. "Wi-Fi 7" or "802.11be".
        limit: Maximum candidates, clamped to 1-50 (default 5).
    """
    return hardware_catalog.search(
        query,
        vendor=vendor,
        include_specs=include_specs,
        include_taa=include_taa,
        multigig_only=multigig_only,
        wifi_standard=wifi_standard,
        limit=limit,
    )


@mcp.tool(annotations=READ_ONLY_LOCAL)
def compare_hardware(devices: list[str]) -> dict[str, Any]:
    """Compare two to five HPE Aruba or HPE Juniper devices without RAG.

    Each entry may be an exact SKU/part-number alias or an unambiguous model.
    The comparison refuses to pick between model variants: if a family such as
    ``CX 6300`` has multiple SKUs, it returns the candidates for selection.
    Compared fields include ports, PoE, uplinks, lifecycle, and every verified
    normalized spec captured in the local SQLite catalog.

    Args:
        devices: Two to five SKU, part-number alias, or model identifiers.
    """
    return hardware_catalog.compare(devices)
