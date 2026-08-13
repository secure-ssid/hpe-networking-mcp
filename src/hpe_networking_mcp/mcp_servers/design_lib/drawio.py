"""Draw.io / diagrams.net XML exporter for network designs."""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from hpe_networking_mcp.mcp_servers.design_lib.icons import style_for_node
from hpe_networking_mcp.mcp_servers.design_lib.model import DiagramModel, layout_positions

# draw.io built-in shape keys (Cisco + generic mxgraph)
_ROLE_SHAPE: dict[str, str] = {
    "cloud": "shape=mxgraph.networks.cloud;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "internet": "shape=mxgraph.networks.cloud;fillColor=#E1D5E7;strokeColor=#9673A6;",
    "firewall": "shape=mxgraph.cisco.security.firewall;fillColor=#F8CECC;strokeColor=#B85450;",
    "router": "shape=mxgraph.cisco.routers.router;fillColor=#FFE6CC;strokeColor=#D79B00;",
    "core_switch": "shape=mxgraph.cisco.switches.workgroup_switch;fillColor=#D5E8D4;strokeColor=#82B366;",
    "agg_switch": "shape=mxgraph.cisco.switches.workgroup_switch;fillColor=#D5E8D4;strokeColor=#82B366;",
    "access_switch": "shape=mxgraph.cisco.switches.workgroup_switch;fillColor=#FFF2CC;strokeColor=#D6B656;",
    "gateway": "shape=mxgraph.cisco.misc.gatekeeper;fillColor=#FFE6CC;strokeColor=#D79B00;",
    "campus_ap": "shape=mxgraph.cisco.wireless.wireless_access_point;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "mist_ap": "shape=mxgraph.cisco.wireless.wireless_access_point;fillColor=#E1D5E7;strokeColor=#9673A6;",
    "clearpass": "shape=mxgraph.cisco.servers.server;fillColor=#F5F5F5;strokeColor=#666666;",
    "controller": "shape=mxgraph.cisco.controllers_and_modules.system_controller;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "server": "shape=mxgraph.cisco.servers.server;fillColor=#F5F5F5;strokeColor=#666666;",
    "client": "shape=mxgraph.cisco.computers_and_peripherals.pc;fillColor=#F5F5F5;strokeColor=#666666;",
    "generic": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;",
}

_LINK_STYLE: dict[str, str] = {
    "ethernet": "endArrow=none;html=1;strokeWidth=2;strokeColor=#666666;",
    "trunk": "endArrow=none;html=1;strokeWidth=3;strokeColor=#1A1A1A;dashed=0;",
    "wireless": "endArrow=none;html=1;strokeWidth=2;strokeColor=#6C8EBF;dashed=1;dashPattern=8 8;",
    "wan": "endArrow=none;html=1;strokeWidth=2;strokeColor=#B85450;dashed=1;",
    "logical": "endArrow=open;html=1;strokeWidth=1;strokeColor=#9673A6;dashed=1;",
}


def export_drawio(model: DiagramModel) -> dict[str, Any]:
    """Return Draw.io XML string + metadata."""
    positions = layout_positions(model)
    cells: list[str] = []
    cells.append('<mxCell id="0"/>')
    cells.append('<mxCell id="1" parent="0"/>')

    # Groups are optional labels only (no giant dashed boxes behind nodes).
    # Full swimlane containers made layouts look noisy ("stuff behind/around").
    cell_id = 2
    group_ids: dict[str, int] = {}
    for group in model.groups:
        members = [m for m in group.members if m in positions]
        if not members:
            continue
        xs = [positions[m][0] for m in members]
        ys = [positions[m][1] for m in members]
        x = min(xs)
        y = min(ys) - 28
        w = max(120, max(xs) - min(xs) + 64)
        gid = cell_id
        group_ids[group.id] = gid
        cell_id += 1
        label = escape(group.label)
        cells.append(
            f'<mxCell id="{gid}" value="{label}" style="text;html=1;strokeColor=none;'
            f'fillColor=none;align=left;verticalAlign=middle;fontStyle=1;fontSize=11;'
            f'fontColor=#666666;" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="20" as="geometry"/>'
            f"</mxCell>"
        )

    node_cell: dict[str, int] = {}
    for node in model.nodes:
        x, y = positions[node.id]
        cid = cell_id
        node_cell[node.id] = cid
        cell_id += 1
        shape = _ROLE_SHAPE.get(node.role, _ROLE_SHAPE["generic"])
        # Prefer generic geometric style when vendor icon pack marks custom
        icon_meta = style_for_node(node.vendor, node.role)
        if icon_meta.get("drawio_style"):
            shape = str(icon_meta["drawio_style"])
        value_parts = [escape(node.label)]
        if node.mgmt_ip:
            value_parts.append(escape(node.mgmt_ip))
        if node.vendor and node.vendor != "generic":
            value_parts.append(escape(node.vendor))
        value = "&#xa;".join(value_parts)
        parent = "1"
        # Compact fixed nodes — no embedded product photos in Draw.io XML.
        cells.append(
            f'<mxCell id="{cid}" value="{value}" style="{shape}verticalLabelPosition=bottom;'
            f'verticalAlign=top;html=1;aspect=fixed;spacingTop=2;" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="56" height="56" as="geometry"/>'
            f"</mxCell>"
        )

    for link in model.links:
        src = node_cell.get(link.source)
        dst = node_cell.get(link.target)
        if src is None or dst is None:
            continue
        cid = cell_id
        cell_id += 1
        style = _LINK_STYLE.get(link.link_type, _LINK_STYLE["ethernet"])
        label = escape(link.label or link.bandwidth or "")
        cells.append(
            f'<mxCell id="{cid}" value="{label}" style="{style}" edge="1" parent="1" '
            f'source="{src}" target="{dst}">'
            f'<mxGeometry relative="1" as="geometry"/>'
            f"</mxCell>"
        )

    title = escape(model.title)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mxfile host="hpe-networking-mcp" type="device">\n'
        f'  <diagram id="design" name="{title}">\n'
        '    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" '
        'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        'pageWidth="1600" pageHeight="1200" math="0" shadow="0">\n'
        "      <root>\n"
        f"        {chr(10).join('        ' + c for c in cells)}\n"
        "      </root>\n"
        "    </mxGraphModel>\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )
    return {
        "format": "drawio",
        "filename_ext": ".drawio",
        "content": xml,
        "content_type": "application/xml",
        "editable_in": ["draw.io", "diagrams.net", "VS Code Draw.io Integration"],
        "title": model.title,
        "node_count": len(model.nodes),
        "link_count": len(model.links),
        "notes": [
            "Cisco mxgraph shapes used where helpful; Aruba/HPE/Mist use generic/Cisco stand-ins "
            "unless you supply icons via HPE_MCP_DIAGRAM_ICON_DIR.",
            "Open the .drawio file in diagrams.net to edit.",
        ],
    }
