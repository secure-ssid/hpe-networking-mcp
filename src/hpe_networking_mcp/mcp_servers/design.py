"""MCP server — optional network design / diagram backend (7 curated tools).

Enabled via tool router env:
  HPE_MCP_PRODUCTS=design
  # or HPE_MCP_TOOLSETS=...,design

No vendor API credentials. Generates local design artifacts only:

  - Draw.io / diagrams.net XML (primary, editable)
  - Graphviz DOT (+ optional SVG/PNG/PDF if `dot` is installed)
  - NeXt UI topology JSON (+ HTML stub)

Icons: generic shapes ship in ``resources/diagram_icons/``. Point
``HPE_MCP_DIAGRAM_ICON_DIR`` at a local vendor pack (do not commit
third-party logos/photos). Juniper/Mist reference library:
https://www.juniper.net/us/en/company/images/image-library-logos-and-product-photos.html

All tools are read-only from a network perspective. Optional file writes are
sandboxed under ``outputs/diagrams/`` when ``save=True``.
"""

from __future__ import annotations

import base64
from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.design_lib.drawio import export_drawio
from hpe_networking_mcp.mcp_servers.design_lib.files import (
    write_bytes_artifact,
    write_json_artifact,
    write_text_artifact,
)
from hpe_networking_mcp.mcp_servers.design_lib.graphviz_export import export_graphviz
from hpe_networking_mcp.mcp_servers.design_lib.icons import list_icons, resolve_icon
from hpe_networking_mcp.mcp_servers.design_lib.model import (
    KNOWN_ROLES,
    KNOWN_VENDORS,
    model_from_central_topology,
    parse_model,
    validate_model,
)
from hpe_networking_mcp.mcp_servers.design_lib.nextui import export_next_ui
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY

mcp = MCPServer("design-core")

_MAX_INLINE = 120_000
_MAX_RESPONSE_CHARS = 180_000


def _maybe_truncate_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep MCP responses bounded when returning large XML/DOT inline."""
    content = payload.get("content")
    if isinstance(content, str) and len(content) > _MAX_INLINE:
        payload = dict(payload)
        payload["content"] = content[:_MAX_INLINE]
        payload["content_truncated"] = True
        payload["content_original_chars"] = len(content)
        payload["note"] = (
            f"Inline content truncated to {_MAX_INLINE} chars; "
            "re-run with save=True to write the full artifact under outputs/diagrams/."
        )
    return payload


def _bound_dict(body: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on oversized local payloads (no HTTP response object)."""
    import json

    encoded = json.dumps(body, ensure_ascii=False, default=str)
    if len(encoded) <= _MAX_RESPONSE_CHARS:
        return body
    # Drop inline content first
    export = body.get("export")
    if isinstance(export, dict) and isinstance(export.get("content"), str):
        body = dict(body)
        export = dict(export)
        export["content"] = export["content"][: max(2000, _MAX_INLINE // 4)]
        export["content_truncated"] = True
        body["export"] = export
        body["response_truncated"] = True
        encoded = json.dumps(body, ensure_ascii=False, default=str)
        if len(encoded) <= _MAX_RESPONSE_CHARS:
            return body
    return {
        "ok": body.get("ok", True),
        "error": "response exceeded size budget; call again with save=True and omit inline needs",
        "saved": body.get("saved"),
        "written": body.get("written"),
        "approach": body.get("approach"),
        "response_truncated": True,
        "response_chars": len(encoded),
    }


def _save_export(
    export: dict[str, Any],
    *,
    stem: str,
    save: bool,
) -> dict[str, Any]:
    written: list[dict[str, Any]] = []
    if not save:
        return {"saved": False, "written": written}

    ext = str(export.get("filename_ext") or ".txt")
    content = export.get("content")
    if isinstance(content, str):
        written.append(write_text_artifact(stem, ext, content))
    elif isinstance(content, (dict, list)):
        written.append(write_json_artifact(stem, ext, content))

    rendered = export.get("rendered")
    if isinstance(rendered, dict):
        r_ext = str(rendered.get("filename_ext") or ".bin")
        if isinstance(rendered.get("content"), str):
            written.append(write_text_artifact(f"{stem}_render", r_ext, rendered["content"]))
        elif isinstance(rendered.get("content_base64"), str):
            raw = base64.b64decode(rendered["content_base64"])
            written.append(write_bytes_artifact(f"{stem}_render", r_ext, raw))

    preview = export.get("preview_html")
    if isinstance(preview, str):
        written.append(
            write_text_artifact(
                f"{stem}_next_preview",
                export.get("preview_html_ext") or ".html",
                preview,
            )
        )

    return {"saved": True, "written": written, "output_dir": "outputs/diagrams"}


@mcp.tool(annotations=READ_ONLY)
def list_diagram_icons() -> dict[str, Any]:
    """List diagram icon pack inventory (SVG/PNG/VSS) for network drawings.

    Searches ``HPE_MCP_DIAGRAM_ICON_DIR`` (if set) and
    ``resources/diagram_icons/``. Does not download anything. Use before
    Draw.io / Graphviz topology exports when choosing vendor role art.
    """
    return list_icons()


@mcp.tool(annotations=READ_ONLY)
def validate_diagram_model(model: dict[str, Any]) -> dict[str, Any]:
    """Validate a network design diagram model before Draw.io/Graphviz export.

    Args:
        model: Object with title, nodes[{id,label,role,vendor,...}],
            links[{source,target,link_type,...}], optional groups/notes.
    """
    return validate_model(model)


@mcp.tool(annotations=READ_ONLY)
def list_diagram_roles_and_vendors() -> dict[str, Any]:
    """List accepted diagram node roles/vendors and export approaches (Draw.io, Graphviz, NeXt)."""
    return {
        "roles": sorted(KNOWN_ROLES),
        "vendors": sorted(KNOWN_VENDORS),
        "approaches": [
            {
                "name": "drawio_network_design_diagram",
                "best_for": "switching, routing, topology (editable)",
                "output": ".drawio XML",
                "icons": "Cisco mxgraph built-ins; generic for Aruba/HPE/ClearPass/Mist",
            },
            {
                "name": "export_graphviz_topology",
                "best_for": "direct image generation, any vendor icon pack",
                "output": "DOT + optional PNG/SVG/PDF",
                "icons": "Any local pack via HPE_MCP_DIAGRAM_ICON_DIR",
            },
            {
                "name": "export_next_ui_topology",
                "best_for": "interactive web dashboards",
                "output": "NeXt UI JSON + HTML stub",
                "icons": "Cisco/generic NeXt device_type icons",
            },
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def drawio_network_design_diagram(
    model: dict[str, Any] | None = None,
    topology: dict[str, Any] | None = None,
    title: str | None = None,
    site_id: str | None = None,
    save: bool = False,
    filename_stem: str = "network_design",
) -> dict[str, Any]:
    """Draw a network design topology diagram as editable Draw.io / diagrams.net XML.

    Primary network design export for switching/routing/wireless topology maps.
    Provide either a structured ``model`` or a Central ``get_topology``-shaped
    ``topology`` payload (optionally with site_id/title). Prefer this over
    Graphviz when the operator needs an editable .drawio drawing.

    Args:
        model: Canonical design model (nodes/links/groups).
        topology: Optional Central topology payload to convert.
        title: Optional title override when converting topology.
        site_id: Optional site id annotation when converting topology.
        save: When True, write ``outputs/diagrams/<stem>.drawio``.
        filename_stem: Safe filename stem for save (default network_design).
    """
    try:
        parsed = _resolve_model(model=model, topology=topology, title=title, site_id=site_id)
        export = export_drawio(parsed)
        save_info = _save_export(export, stem=filename_stem, save=save)
        body = {
            "ok": True,
            "approach": "drawio_network_design_diagram",
            "export": _maybe_truncate_content(export),
            **save_info,
        }
        return _bound_dict(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def export_graphviz_topology(
    model: dict[str, Any] | None = None,
    topology: dict[str, Any] | None = None,
    title: str | None = None,
    site_id: str | None = None,
    rankdir: str = "TB",
    render_format: str | None = None,
    save: bool = False,
    filename_stem: str = "network_graph",
) -> dict[str, Any]:
    """Export a Graphviz DOT topology diagram; optional SVG/PNG/PDF via ``dot``.

    Use for direct image generation of network design diagrams with local
    vendor icon packs. Prefer ``drawio_network_design_diagram`` when an editable
    Draw.io drawing is needed instead of Graphviz.

    Args:
        model: Canonical design model.
        topology: Optional Central topology payload to convert.
        title: Optional title override when converting topology.
        site_id: Optional site id when converting topology.
        rankdir: Graphviz rankdir TB|LR|BT|RL.
        render_format: Optional svg|png|pdf (requires Graphviz ``dot`` on PATH).
        save: Write artifacts under outputs/diagrams/.
        filename_stem: Safe filename stem when save=True.
    """
    try:
        parsed = _resolve_model(model=model, topology=topology, title=title, site_id=site_id)
        export = export_graphviz(parsed, rankdir=rankdir, render_format=render_format)
        save_info = _save_export(export, stem=filename_stem, save=save)
        body = {
            "ok": True,
            "approach": "export_graphviz_topology",
            "export": _maybe_truncate_content(export),
            **save_info,
        }
        return _bound_dict(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def export_next_ui_topology(
    model: dict[str, Any] | None = None,
    topology: dict[str, Any] | None = None,
    title: str | None = None,
    site_id: str | None = None,
    save: bool = False,
    filename_stem: str = "network_next",
) -> dict[str, Any]:
    """Export interactive NeXt UI network topology JSON (+ HTML preview stub).

    Use for web dashboards and clickable topology maps. For editable drawings
    use ``drawio_network_design_diagram``; for Graphviz images use
    ``export_graphviz_topology``.

    Args:
        model: Canonical design model.
        topology: Optional Central topology payload to convert.
        title: Optional title override when converting topology.
        site_id: Optional site id when converting topology.
        save: Write ``.next.json`` and preview HTML under outputs/diagrams/.
        filename_stem: Safe filename stem when save=True.
    """
    try:
        parsed = _resolve_model(model=model, topology=topology, title=title, site_id=site_id)
        export = export_next_ui(parsed)
        save_info = _save_export(export, stem=filename_stem, save=save)
        body = {
            "ok": True,
            "approach": "export_next_ui_topology",
            "export": export,
            **save_info,
        }
        return _bound_dict(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def resolve_diagram_icon(vendor: str = "generic", role: str = "generic") -> dict[str, Any]:
    """Resolve the best local diagram icon path for a vendor + role pair."""
    return resolve_icon(vendor, role)


def _resolve_model(
    *,
    model: dict[str, Any] | None,
    topology: dict[str, Any] | None,
    title: str | None,
    site_id: str | None,
):
    if model is not None and topology is not None:
        raise ValueError("pass model or topology, not both")
    if model is not None:
        return parse_model(model)
    if topology is not None:
        return model_from_central_topology(topology, title=title, site_id=site_id)
    raise ValueError("either model or topology is required")
