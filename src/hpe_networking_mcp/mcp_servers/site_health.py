"""MCP server — bounded cross-platform site health (1 tool).

The tool in this module is a small operator seam, not a new vendor API
adapter. It composes the existing Central get_site_health_summary and
Mist mist_get_site_assurance_snapshot reads and keeps their platform
results separate. Central and Mist identifiers are intentionally supplied
separately: this first slice does not guess that a Central site ID, name, or
record maps to a Mist site.

RF summary is not included yet. The existing Central and Mist RF responses do
not share a verified field contract, so inventing a normalized RF shape here
would turn an unavailable metric into fabricated health.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers import mist, monitoring
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY, clamp_limit

mcp = MCPServer("site-health")

_MAX_ERROR_CHARS = 240


def _error_text(prefix: str, error: Any) -> str:
    text = str(error)
    if len(text) > _MAX_ERROR_CHARS:
        text = f"{text[:_MAX_ERROR_CHARS]}..."
    return f"{prefix}: {text}"


def _not_requested(platform: str, identifier: str) -> dict[str, Any]:
    return {
        "status": "not_requested",
        "health": None,
        "errors": [],
        "reason": (
            f"{platform} {identifier} was not supplied; no cross-platform "
            "site mapping is inferred."
        ),
    }


def _central_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "unavailable",
            "health": None,
            "errors": ["Central returned a non-object site-health response."],
        }
    if result.get("error"):
        return {
            "status": "unavailable",
            "health": None,
            "errors": [_error_text("Central site health unavailable", result["error"])],
        }
    errors = [
        _error_text("Central site health", error)
        for error in result.get("errors", [])
        if error
    ][:10]
    return {
        "status": "degraded" if errors else "available",
        "health": result,
        "errors": errors,
    }


def _mist_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "unavailable",
            "health": None,
            "errors": ["Mist returned a non-object site-assurance response."],
        }
    if result.get("error"):
        return {
            "status": "unavailable",
            "health": None,
            "errors": [_error_text("Mist site health unavailable", result["error"])],
        }

    sections = result.get("sections")
    if not isinstance(sections, dict) or not sections:
        return {
            "status": "unavailable",
            "health": None,
            "errors": ["Mist returned no site-assurance sections."],
        }

    errors = []
    successful_sections = []
    for name, section in sections.items():
        if not isinstance(section, dict):
            errors.append(f"Mist {name} section returned a non-object response.")
            continue
        if section.get("error"):
            errors.append(_error_text(f"Mist {name} section unavailable", section["error"]))
            continue
        status_code = section.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            errors.append(f"Mist {name} section unavailable: HTTP {status_code}.")
            continue
        successful_sections.append(section)
    if not successful_sections:
        return {"status": "unavailable", "health": None, "errors": errors}
    return {
        "status": "degraded" if errors else "available",
        "health": result,
        "errors": errors,
    }


def _overall_status(platforms: dict[str, dict[str, Any]]) -> str:
    statuses = [platform["status"] for platform in platforms.values()]
    available = [status for status in statuses if status in {"available", "degraded"}]
    if not available:
        return "unavailable"
    if any(status in {"degraded", "unavailable"} for status in statuses):
        return "degraded"
    return "available"


@mcp.tool(annotations=READ_ONLY)
async def get_site_health(
    central_site_id: str | None = None,
    central_site_name: str | None = None,
    mist_site_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get bounded site health from configured Central and Mist platforms.

    Central and Mist reads run independently, so one unavailable platform does
    not hide the other platform's result. Platform identifiers are separate;
    this tool never guesses a Central-to-Mist site mapping.

    Args:
        central_site_id: Central site ID for the existing Central health read.
        central_site_name: Central site name when a Central ID is unavailable.
        mist_site_id: Mist site ID for the existing Mist assurance snapshot.
        limit: Maximum items requested from Mist assurance sections, clamped to
            the shared 1..200 limit. Central's curated summary has its own
            fixed bounds.

    Returns:
        A bounded platforms result. health is None whenever a platform is
        unavailable or was not requested; inspect errors and status before
        treating any platform as usable.
    """
    central_id = central_site_id.strip() if central_site_id else ""
    central_name = central_site_name.strip() if central_site_name else ""
    mist_id = mist_site_id.strip() if mist_site_id else ""
    if not central_id and not central_name and not mist_id:
        return {
            "status": "unavailable",
            "platforms": {
                "central": _not_requested("Central", "site identifier"),
                "mist": _not_requested("Mist", "site ID"),
            },
            "errors": [
                "Provide central_site_id, central_site_name, or mist_site_id."
            ],
        }

    platforms: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    calls: dict[str, Any] = {}

    if central_id or central_name:
        calls["central"] = asyncio.to_thread(
            monitoring.get_site_health_summary,
            site_id=central_id or None,
            site_name=central_name or None,
        )
    else:
        platforms["central"] = _not_requested("Central", "site identifier")
        warnings.append(platforms["central"]["reason"])

    mist_status = mist.mist_status()
    if mist_id and mist_status.get("configured"):
        calls["mist"] = mist.mist_get_site_assurance_snapshot(
            mist_id,
            limit=clamp_limit(limit),
        )
    elif mist_id:
        platforms["mist"] = {
            "status": "unavailable",
            "health": None,
            "errors": [
                "Mist is not configured. Set MIST_HOST and MIST_API_TOKEN."
            ],
        }
    else:
        platforms["mist"] = _not_requested("Mist", "site ID")
        warnings.append(platforms["mist"]["reason"])

    if calls:
        results = await asyncio.gather(*calls.values(), return_exceptions=True)
        for platform, result in zip(calls, results, strict=True):
            if isinstance(result, Exception):
                platforms[platform] = {
                    "status": "unavailable",
                    "health": None,
                    "errors": [_error_text(f"{platform.title()} site health failed", result)],
                }
            elif platform == "central":
                platforms[platform] = _central_result(result)
            else:
                platforms[platform] = _mist_result(result)

    errors = [
        error
        for platform in platforms.values()
        for error in platform.get("errors", [])
    ]
    return {
        "status": _overall_status(platforms),
        "platforms": platforms,
        "errors": errors,
        "warnings": warnings,
    }


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
