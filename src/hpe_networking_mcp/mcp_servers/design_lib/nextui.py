"""NeXt UI topology JSON exporter (Cisco NeXt / next-ui)."""

from __future__ import annotations

from typing import Any

from hpe_networking_mcp.mcp_servers.design_lib.model import DiagramModel, layout_positions

# NeXt built-in device icons (when using next-ui defaults)
_ROLE_ICON: dict[str, str] = {
    "cloud": "cloud",
    "internet": "cloud",
    "firewall": "firewall",
    "router": "router",
    "core_switch": "switch",
    "agg_switch": "switch",
    "access_switch": "switch",
    "gateway": "router",
    "campus_ap": "wlc",
    "mist_ap": "wlc",
    "clearpass": "server",
    "controller": "groups",
    "server": "server",
    "client": "host",
    "generic": "unknown",
}


def export_next_ui(model: DiagramModel) -> dict[str, Any]:
    """Export a topology object consumable by NeXt UI topology widget."""
    positions = layout_positions(model, col_width=120, row_height=100)
    nodes = []
    for node in model.nodes:
        x, y = positions[node.id]
        nodes.append(
            {
                "id": node.id,
                "name": node.label,
                "device_type": _ROLE_ICON.get(node.role, "unknown"),
                "vendor": node.vendor,
                "role": node.role,
                "x": x,
                "y": y,
                **({"site": node.site} if node.site else {}),
                **({"serial": node.serial} if node.serial else {}),
                **({"mgmt_ip": node.mgmt_ip} if node.mgmt_ip else {}),
            }
        )
    links = []
    for i, link in enumerate(model.links):
        links.append(
            {
                "id": f"link_{i}",
                "source": link.source,
                "target": link.target,
                "name": link.label or link.bandwidth or link.link_type,
                "linkType": link.link_type,
            }
        )

    topology = {
        "nodes": nodes,
        "links": links,
        "groups": [g.to_dict() for g in model.groups],
    }
    # Minimal HTML shell for local preview (no external CDN fetch at runtime by default)
    html = _preview_html(model.title)

    return {
        "format": "next_ui",
        "filename_ext": ".next.json",
        "content": topology,
        "content_type": "application/json",
        "title": model.title,
        "node_count": len(model.nodes),
        "link_count": len(model.links),
        "preview_html": html,
        "preview_html_ext": ".html",
        "notes": [
            "JSON matches a NeXt UI topology data shape (nodes/links with device_type).",
            "preview_html is a stub shell — load NeXt UI assets yourself or paste JSON into an "
            "existing dashboard.",
            "Cisco/generic device_type icons; supply custom icons in your NeXt app for "
            "Aruba/HPE/Mist.",
        ],
    }


def _preview_html(title: str) -> str:
    safe = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{safe} — NeXt UI stub</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
    code {{ background: #f4f4f4; padding: 0.2rem 0.4rem; }}
  </style>
</head>
<body>
  <h1>{safe}</h1>
  <p>This is a local stub produced by hpe-networking-mcp <code>export_next_ui_topology</code>.</p>
  <p>Load the sibling <code>.next.json</code> topology into your NeXt UI application
     (or Cisco NeXt toolkit) to render an interactive canvas.</p>
  <p>hpe-networking-mcp intentionally does not bundle NeXt UI runtime assets.</p>
</body>
</html>
"""
