"""Self-contained HTML/SVG preview renderer for network topology diagrams."""

from __future__ import annotations

import html
import json
import math
from typing import Any

from hpe_networking_mcp.mcp_servers.design_lib.model import DiagramModel, layout_positions


def safe_json_script_embed(data: Any) -> str:
    """Safely embed JSON in a script tag by escaping script tags, ampersands, and line breaks."""
    raw = json.dumps(data, ensure_ascii=False)
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _is_obstructed(x1: float, y1: float, x2: float, y2: float, nx: float, ny: float) -> bool:
    """Check if node center (nx, ny) obstructs straight line between (x1, y1) and (x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-6:
        return False
    t = ((nx - x1) * dx + (ny - y1) * dy) / length_sq
    if t <= 0.1 or t >= 0.9:
        return False
    px = x1 + t * dx
    py = y1 + t * dy
    dist_sq = (nx - px) ** 2 + (ny - py) ** 2
    return dist_sq < 65 * 65


def _place_link_label(
    x: float,
    y: float,
    label: str,
    occupied: list[tuple[float, float, float, float]],
) -> tuple[float, float]:
    """Keep labels clear of nodes, group headings, and previously placed labels."""
    half_width = (11 * len(label) + 8) / 2
    lateral = half_width + 12
    offsets = (
        (0, 0),
        (0, -24),
        (0, 24),
        (lateral, 0),
        (-lateral, 0),
        (0, -48),
        (0, 48),
        (lateral, -24),
        (-lateral, -24),
        (lateral, 24),
        (-lateral, 24),
    )
    for dx, dy in offsets:
        left, top = x + dx - half_width, y + dy - 14
        right, bottom = x + dx + half_width, y + dy + 4
        if not any(
            left < box_right and right > box_left and top < box_bottom and bottom > box_top
            for box_left, box_top, box_right, box_bottom in occupied
        ):
            occupied.append((left, top, right, bottom))
            return x + dx, y + dy
    x = max(box[2] for box in occupied) + half_width + 12
    occupied.append((x - half_width, y - 14, x + half_width, y + 4))
    return x, y


def render_topology_html(model: DiagramModel) -> str:
    """Render a self-contained, offline HTML/SVG topology viewer from DiagramModel."""
    title_safe = html.escape(model.title)
    positions = layout_positions(model, col_width=180, row_height=120)
    occupied: list[tuple[float, float, float, float]] = [
        (x - 64, y - 32, x + 64, y + 32) for x, y in positions.values()
    ]

    # Initial bounds based on node positions
    if positions:
        xs = [pos[0] for pos in positions.values()]
        ys = [pos[1] for pos in positions.values()]
        min_x: float = min(xs) - 80
        max_x: float = max(xs) + 80
        min_y: float = min(ys) - 75
        max_y: float = max(ys) + 60
    else:
        min_x, max_x, min_y, max_y = 0, 800, 0, 600

    # Build SVG Group elements
    groups_svg = []
    for g in model.groups:
        member_pos = [positions[m] for m in g.members if m in positions]
        if member_pos:
            g_min_x = min(p[0] for p in member_pos) - 75
            g_max_x = max(p[0] for p in member_pos) + 75
            g_min_y = min(p[1] for p in member_pos) - 65
            g_max_y = max(p[1] for p in member_pos) + 50
            g_w = g_max_x - g_min_x
            g_h = g_max_y - g_min_y
            g_id = html.escape(g.id)
            g_label = html.escape(g.label)
            occupied.append(
                (g_min_x + 10, g_min_y + 8, g_min_x + 22 + 14 * len(g.label), g_min_y + 28)
            )

            min_x = min(min_x, g_min_x)
            max_x = max(max_x, g_max_x)
            min_y = min(min_y, g_min_y)
            max_y = max(max_y, g_max_y)

            groups_svg.append(
                f'<g class="topology-group" id="group-{g_id}" data-id="{g_id}">\n'
                f'  <rect x="{g_min_x}" y="{g_min_y}" width="{g_w}" height="{g_h}" '
                f'rx="10" ry="10" class="group-box"/>\n'
                f'  <text x="{g_min_x + 14}" y="{g_min_y + 24}" class="group-label">'
                f"{g_label}</text>\n"
                f"</g>"
            )

    # Build SVG Link elements (routing around obstructing nodes)
    links_svg = []
    for i, link in enumerate(model.links):
        if link.source in positions and link.target in positions:
            x1, y1 = positions[link.source]
            x2, y2 = positions[link.target]
            src_safe = html.escape(link.source)
            dst_safe = html.escape(link.target)
            type_safe = html.escape(link.link_type)
            lbl_text = link.label or link.bandwidth or ""
            lbl_safe = html.escape(lbl_text)
            link_id = f"link-{i}-{src_safe}-{dst_safe}"

            # Check for obstructing nodes along straight path
            obstructed = False
            for node in model.nodes:
                if node.id != link.source and node.id != link.target and node.id in positions:
                    nx, ny = positions[node.id]
                    if _is_obstructed(x1, y1, x2, y2, nx, ny):
                        obstructed = True
                        break

            if obstructed:
                dx, dy = x2 - x1, y2 - y1
                length = math.hypot(dx, dy)
                if length > 0:
                    px, py = -dy / length, dx / length
                    if px < 0 or (px == 0 and py < 0):
                        px, py = -px, -py
                else:
                    px, py = 1.0, 0.0

                offset = 150.0
                mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                cx, cy = mx + px * offset, my + py * offset

                min_x = min(min_x, cx - 20)
                max_x = max(max_x, cx + 20)
                min_y = min(min_y, cy - 20)
                max_y = max(max_y, cy + 20)

                # Point at t=0.5 on quadratic curve
                lx = 0.25 * x1 + 0.5 * cx + 0.25 * x2
                ly = 0.25 * y1 + 0.5 * cy + 0.25 * y2 - 6
                if lbl_safe:
                    lx, ly = _place_link_label(lx, ly, lbl_text, occupied)

                links_svg.append(
                    f'<g class="topology-link-group" id="{link_id}">\n'
                    f"  <title>{src_safe} -&gt; {dst_safe}: {lbl_safe}</title>\n"
                    f'  <path d="M {x1} {y1} Q {cx} {cy} {x2} {y2}" '
                    f'class="topology-link link-type-{type_safe}" '
                    f'data-source="{src_safe}" data-target="{dst_safe}" '
                    f'data-type="{type_safe}" marker-end="url(#arrow)"/>\n'
                    + (
                        f'  <text x="{lx}" y="{ly}" class="link-label" '
                        f'text-anchor="middle">{lbl_safe}</text>\n'
                        if lbl_safe
                        else ""
                    )
                    + "</g>"
                )
            else:
                mid_x = (x1 + x2) / 2
                mid_y = (y1 + y2) / 2 - 6
                if lbl_safe:
                    mid_x, mid_y = _place_link_label(mid_x, mid_y, lbl_text, occupied)
                links_svg.append(
                    f'<g class="topology-link-group" id="{link_id}">\n'
                    f"  <title>{src_safe} -&gt; {dst_safe}: {lbl_safe}</title>\n"
                    f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                    f'class="topology-link link-type-{type_safe}" '
                    f'data-source="{src_safe}" data-target="{dst_safe}" '
                    f'data-type="{type_safe}" marker-end="url(#arrow)"/>\n'
                    + (
                        f'  <text x="{mid_x}" y="{mid_y}" class="link-label" '
                        f'text-anchor="middle">{lbl_safe}</text>\n'
                        if lbl_safe
                        else ""
                    )
                    + "</g>"
                )

    # Build SVG Node elements
    nodes_svg = []
    for node in model.nodes:
        if node.id in positions:
            x, y = positions[node.id]
            nid_safe = html.escape(node.id)
            nlbl_safe = html.escape(node.label)
            role_safe = html.escape(node.role)
            vendor_safe = html.escape(node.vendor)
            site_safe = html.escape(node.site or "")
            mgmt_safe = html.escape(node.mgmt_ip or "")
            serial_safe = html.escape(node.serial or "")
            nodes_svg.append(
                f'<g class="topology-node role-{role_safe} vendor-{vendor_safe}" '
                f'id="node-{nid_safe}" transform="translate({x}, {y})" '
                f'tabindex="0" role="button" '
                f'aria-label="Node {nlbl_safe}, {role_safe}, {vendor_safe}" '
                f'data-id="{nid_safe}" data-label="{nlbl_safe}" data-role="{role_safe}" '
                f'data-vendor="{vendor_safe}" data-site="{site_safe}" '
                f'data-mgmt="{mgmt_safe}" data-serial="{serial_safe}">\n'
                f'  <rect x="-60" y="-28" width="120" height="56" rx="8" ry="8" '
                f'class="node-body"/>\n'
                f'  <text x="0" y="-6" class="node-title" text-anchor="middle">{nlbl_safe}</text>\n'
                f'  <text x="0" y="14" class="node-subtitle" text-anchor="middle">'
                f"{role_safe} | {vendor_safe}</text>\n"
                f"</g>"
            )

    if occupied:
        min_x = min(min_x, min(box[0] for box in occupied))
        min_y = min(min_y, min(box[1] for box in occupied))
        max_x = max(max_x, max(box[2] for box in occupied))
        max_y = max(max_y, max(box[3] for box in occupied))
    pad = 30
    vb_x = int(min_x - pad)
    vb_y = int(min_y - pad)
    vb_w = max(400, int((max_x - min_x) + 2 * pad))
    vb_h = max(300, int((max_y - min_y) + 2 * pad))

    # Build static HTML tables fallback
    nodes_rows = []
    for n in model.nodes:
        nodes_rows.append(
            f'<tr class="table-node-row" data-id="{html.escape(n.id)}">\n'
            f"  <td><code>{html.escape(n.id)}</code></td>\n"
            f"  <td>{html.escape(n.label)}</td>\n"
            f'  <td><span class="badge role-badge">{html.escape(n.role)}</span></td>\n'
            f"  <td>{html.escape(n.vendor)}</td>\n"
            f"  <td>{html.escape(n.site or '-')}</td>\n"
            f"  <td>{html.escape(n.mgmt_ip or '-')}</td>\n"
            f"  <td>{html.escape(n.serial or '-')}</td>\n"
            f"</tr>"
        )

    links_rows = []
    for lnk in model.links:
        lbl = lnk.label or lnk.bandwidth or "-"
        links_rows.append(
            f"<tr>\n"
            f"  <td><code>{html.escape(lnk.source)}</code></td>\n"
            f"  <td><code>{html.escape(lnk.target)}</code></td>\n"
            f'  <td><span class="badge link-badge">{html.escape(lnk.link_type)}</span></td>\n'
            f"  <td>{html.escape(lbl)}</td>\n"
            f"</tr>"
        )

    groups_rows = []
    for g in model.groups:
        members_str = ", ".join(f"<code>{html.escape(m)}</code>" for m in g.members) or "-"
        groups_rows.append(
            f"<tr>\n"
            f"  <td><code>{html.escape(g.id)}</code></td>\n"
            f"  <td>{html.escape(g.label)}</td>\n"
            f"  <td>{members_str}</td>\n"
            f"</tr>"
        )

    notes_html = ""
    if model.notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in model.notes)
        notes_html = f'<div class="card notes-card"><h3>Design Notes</h3><ul>{items}</ul></div>'

    model_dict = model.to_dict()
    embedded_json = safe_json_script_embed(model_dict)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title_safe} — Network Topology Viewer</title>
  <style>
    :root {{
      --bg-color: #f8f9fa;
      --text-color: #212529;
      --card-bg: #ffffff;
      --border-color: #dee2e6;
      --node-bg: #ffffff;
      --node-stroke: #495057;
      --node-selected: #0d6efd;
      --link-stroke: #6c757d;
      --group-bg: rgba(13, 110, 253, 0.04);
      --group-stroke: #0d6efd;
      --badge-bg: #e9ecef;
      --banner-bg: #fff3cd;
      --banner-text: #664d03;
      --banner-border: #ffecb5;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg-color: #121212;
        --text-color: #e0e0e0;
        --card-bg: #1e1e1e;
        --border-color: #333333;
        --node-bg: #2b2b2b;
        --node-stroke: #90caf9;
        --node-selected: #90caf9;
        --link-stroke: #b0bec5;
        --group-bg: rgba(144, 202, 249, 0.06);
        --group-stroke: #90caf9;
        --badge-bg: #333333;
        --banner-bg: #332701;
        --banner-text: #ffda6a;
        --banner-border: #664d03;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg-color);
      color: var(--text-color);
      margin: 0;
      padding: 1rem;
      line-height: 1.5;
      max-width: 100%;
    }}
    @media (max-width: 600px) {{
      body {{ padding: 0.5rem; }}
    }}
    header {{ margin-bottom: 1.5rem; }}
    h1 {{ margin: 0 0 0.5rem 0; font-size: 1.75rem; }}
    .review-banner {{
      background-color: var(--banner-bg);
      color: var(--banner-text);
      border: 1px solid var(--banner-border);
      padding: 0.75rem 1rem;
      border-radius: 6px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
    }}
    .stats-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }}
    .stat-pill {{
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      padding: 0.25rem 0.75rem;
      border-radius: 16px;
      font-size: 0.85rem;
      font-weight: 500;
    }}
    .controls-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1.5rem;
      max-width: 100%;
    }}
    .search-box {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-grow: 1;
      max-width: 100%;
    }}
    .search-box input {{
      width: 100%;
      padding: 0.4rem 0.75rem;
      border: 1px solid var(--border-color);
      border-radius: 4px;
      background: var(--bg-color);
      color: var(--text-color);
    }}
    .btn {{
      padding: 0.4rem 0.8rem;
      border: 1px solid var(--border-color);
      background: var(--card-bg);
      color: var(--text-color);
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 500;
    }}
    .btn:hover {{ opacity: 0.9; background: var(--badge-bg); }}
    .btn-group {{ display: flex; gap: 0.25rem; }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    @media (max-width: 900px) {{
      .workspace {{ grid-template-columns: 1fr; }}
    }}
    .svg-container {{
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      overflow: hidden;
      height: clamp(320px, 65vh, 720px);
      min-height: 0;
      position: relative;
      max-width: 100%;
    }}
    svg.topology-canvas {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: block;
    }}
    .group-box {{
      fill: var(--group-bg);
      stroke: var(--group-stroke);
      stroke-dasharray: 4 4;
      stroke-width: 1.5;
    }}
    .group-label {{
      fill: var(--text-color);
      font-size: 13px;
      font-weight: 600;
      opacity: 0.85;
    }}
    .topology-link {{
      stroke: var(--link-stroke);
      stroke-width: 2;
      fill: none;
      transition: opacity 0.2s;
    }}
    .topology-link.link-type-wireless {{ stroke-dasharray: 6 3; }}
    .topology-link.link-type-logical {{ stroke-dasharray: 2 2; }}
    .link-label {{
      fill: var(--text-color);
      font-size: 11px;
      opacity: 0.8;
    }}
    .topology-node {{
      cursor: pointer;
      outline: none;
      transition: transform 0.15s, opacity 0.2s;
    }}
    .node-body {{
      fill: var(--node-bg);
      stroke: var(--node-stroke);
      stroke-width: 2;
    }}
    .topology-node.selected .node-body,
    .topology-node:focus .node-body,
    .topology-node:focus-visible .node-body {{
      stroke: var(--node-selected);
      stroke-width: 3.5px;
    }}
    .node-title {{
      fill: var(--text-color);
      font-size: 12px;
      font-weight: 600;
    }}
    .node-subtitle {{
      fill: var(--text-color);
      font-size: 10px;
      opacity: 0.7;
    }}
    .dimmed {{ opacity: 0.15; }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 1rem;
      margin-bottom: 1.5rem;
      min-width: 0;
      max-width: 100%;
      overflow: hidden;
    }}
    .card h3 {{
      margin-top: 0;
      font-size: 1.1rem;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 0.5rem;
    }}
    .detail-prop {{ margin-bottom: 0.5rem; font-size: 0.9rem; }}
    .detail-prop strong {{
      display: inline-block;
      width: 90px;
      color: var(--text-color);
      opacity: 0.8;
    }}
    .table-wrapper {{
      display: block;
      width: 100%;
      min-width: 0;
      max-width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      margin-bottom: 1rem;
      border: 1px solid var(--border-color);
      border-radius: 4px;
    }}
    .table-wrapper:focus {{
      outline: 2px solid var(--node-selected);
    }}
    table.data-table {{
      width: 100%;
      min-width: 500px;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }}
    table.data-table th, table.data-table td {{
      padding: 0.5rem 0.75rem;
      border-bottom: 1px solid var(--border-color);
    }}
    table.data-table th {{
      background: var(--badge-bg);
      font-weight: 600;
    }}
    table.data-table tr.table-node-row {{ cursor: pointer; }}
    table.data-table tr.table-node-row:hover {{ background: var(--badge-bg); }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      background: var(--badge-bg);
      font-size: 0.75rem;
    }}
    code {{ font-family: monospace; font-size: 0.85em; }}
  </style>
</head>
<body>
  <header>
    <h1>{title_safe}</h1>
    <div class="review-banner">
      <strong>Operator-supplied review artifact:</strong>
      Not validated live network state or vendor-certified design.
    </div>
    <div class="stats-bar">
      <span class="stat-pill">Nodes: <strong id="stat-nodes">{len(model.nodes)}</strong></span>
      <span class="stat-pill">Links: <strong id="stat-links">{len(model.links)}</strong></span>
      <span class="stat-pill">Groups: <strong id="stat-groups">{len(model.groups)}</strong></span>
    </div>
  </header>

  <div class="controls-bar">
    <div class="search-box">
      <label for="search-input">Search:</label>
      <input type="text" id="search-input" placeholder="Filter nodes..."
             aria-label="Search topology nodes"/>
    </div>
    <div class="btn-group">
      <button type="button" class="btn" id="btn-zoom-in"
              title="Zoom in" aria-label="Zoom in">+ Zoom In</button>
      <button type="button" class="btn" id="btn-zoom-out"
              title="Zoom out" aria-label="Zoom out">- Zoom Out</button>
      <button type="button" class="btn" id="btn-zoom-reset"
              title="Reset view" aria-label="Reset view">Reset</button>
    </div>
    <button type="button" class="btn" id="btn-download-svg"
            title="Download SVG" aria-label="Download SVG">Download SVG</button>
  </div>

  <div class="workspace">
    <div class="svg-container">
      <svg id="topology-svg" class="topology-canvas" viewBox="{vb_x} {vb_y} {vb_w} {vb_h}"
           xmlns="http://www.w3.org/2000/svg" aria-label="Topology Diagram">
        <title>{title_safe} — Network Topology Diagram</title>
        <desc>Topology diagram displaying {len(model.nodes)} nodes,
              {len(model.links)} links, and {len(model.groups)} groups.</desc>
        <defs>
          <style>
            .group-box {{
              fill: #f8f9fa; fill-opacity: 0.6; stroke: #0d6efd;
              stroke-dasharray: 4 4; stroke-width: 1.5px;
            }}
            .group-label {{
              fill: #212529; font-family: system-ui, -apple-system, sans-serif;
              font-size: 13px; font-weight: 600;
            }}
            .topology-link {{ stroke: #6c757d; stroke-width: 2px; fill: none; }}
            .topology-link.link-type-wireless {{ stroke-dasharray: 6 3; }}
            .topology-link.link-type-logical {{ stroke-dasharray: 2 2; }}
            .link-label {{
              fill: #212529; font-family: system-ui, -apple-system, sans-serif; font-size: 11px;
            }}
            .node-body {{ fill: #ffffff; stroke: #495057; stroke-width: 2px; }}
            .node-title {{
              fill: #212529; font-family: system-ui, -apple-system, sans-serif;
              font-size: 12px; font-weight: 600;
            }}
            .node-subtitle {{
              fill: #6c757d; font-family: system-ui, -apple-system, sans-serif; font-size: 10px;
            }}
            @media (prefers-color-scheme: dark) {{
              .group-box {{ fill: #1e1e1e; fill-opacity: 0.6; stroke: #90caf9; }}
              .group-label {{ fill: #e0e0e0; }}
              .topology-link {{ stroke: #b0bec5; }}
              .link-label {{ fill: #e0e0e0; }}
              .node-body {{ fill: #2b2b2b; stroke: #90caf9; }}
              .node-title {{ fill: #e0e0e0; }}
              .node-subtitle {{ fill: #aaa; }}
            }}
          </style>
          <marker id="arrow" viewBox="0 0 10 10" refX="28" refY="5" markerWidth="6"
                  markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#6c757d"/>
          </marker>
        </defs>
        <g id="svg-viewport">
          <g class="groups-layer">
            {"".join(groups_svg)}
          </g>
          <g class="links-layer">
            {"".join(links_svg)}
          </g>
          <g class="nodes-layer">
            {"".join(nodes_svg)}
          </g>
        </g>
      </svg>
    </div>

    <div class="sidebar">
      <div class="card" id="details-card">
        <h3>Node Details</h3>
        <div id="details-content">
          <p style="opacity:0.7; font-size:0.9rem;">
            Select a node in the diagram or table to inspect details.
          </p>
        </div>
      </div>
    </div>
  </div>

  {notes_html}

  <div class="card">
    <h3>Topology Data (Text / Table Fallback)</h3>
    <h4>Nodes ({len(model.nodes)})</h4>
    <div class="table-wrapper" tabindex="0" role="region" aria-label="Nodes table">
      <table class="data-table" id="nodes-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Label</th>
            <th>Role</th>
            <th>Vendor</th>
            <th>Site</th>
            <th>Mgmt IP</th>
            <th>Serial</th>
          </tr>
        </thead>
        <tbody>
          {"".join(nodes_rows) if nodes_rows else '<tr><td colspan="7">No nodes defined</td></tr>'}
        </tbody>
      </table>
    </div>

    <h4 style="margin-top: 1.5rem;">Links ({len(model.links)})</h4>
    <div class="table-wrapper" tabindex="0" role="region" aria-label="Links table">
      <table class="data-table">
        <thead>
          <tr>
            <th>Source</th>
            <th>Target</th>
            <th>Type</th>
            <th>Label / Bandwidth</th>
          </tr>
        </thead>
        <tbody>
          {"".join(links_rows) if links_rows else '<tr><td colspan="4">No links defined</td></tr>'}
        </tbody>
      </table>
    </div>

    <h4 style="margin-top: 1.5rem;">Groups ({len(model.groups)})</h4>
    <div class="table-wrapper" tabindex="0" role="region" aria-label="Groups table">
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Label</th>
            <th>Members</th>
          </tr>
        </thead>
        <tbody>
        {"".join(groups_rows) if groups_rows else '<tr><td colspan="3">No groups defined</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const TOPOLOGY_DATA = {embedded_json};

    let currentScale = 1.0;
    const viewportGroup = document.getElementById('svg-viewport');

    function updateTransform() {{
      if (viewportGroup) {{
        viewportGroup.setAttribute('transform', 'scale(' + currentScale + ')');
      }}
    }}

    document.getElementById('btn-zoom-in')?.addEventListener('click', function() {{
      currentScale = Math.min(currentScale * 1.2, 3.0);
      updateTransform();
    }});

    document.getElementById('btn-zoom-out')?.addEventListener('click', function() {{
      currentScale = Math.max(currentScale / 1.2, 0.4);
      updateTransform();
    }});

    document.getElementById('btn-zoom-reset')?.addEventListener('click', function() {{
      currentScale = 1.0;
      updateTransform();
    }});

    function selectNode(nodeId) {{
      const nodes = TOPOLOGY_DATA.nodes || [];
      const node = nodes.find(n => n.id === nodeId);
      const detailsContainer = document.getElementById('details-content');

      document.querySelectorAll('.topology-node').forEach(el => {{
        if (el.getAttribute('data-id') === nodeId) {{
          el.classList.add('selected');
        }} else {{
          el.classList.remove('selected');
        }}
      }});

      if (!detailsContainer) return;
      if (!node) {{
        detailsContainer.textContent = 'Node details not found.';
        return;
      }}

      detailsContainer.innerHTML = '';

      const props = [
        ['ID', node.id],
        ['Label', node.label],
        ['Role', node.role],
        ['Vendor', node.vendor],
        ['Site', node.site || '-'],
        ['Mgmt IP', node.mgmt_ip || '-'],
        ['Serial', node.serial || '-']
      ];

      props.forEach(([key, val]) => {{
        const div = document.createElement('div');
        div.className = 'detail-prop';
        const strong = document.createElement('strong');
        strong.textContent = key + ': ';
        const span = document.createElement('span');
        span.textContent = val;
        div.appendChild(strong);
        div.appendChild(span);
        detailsContainer.appendChild(div);
      }});

      const connectedLinks = (TOPOLOGY_DATA.links || []).filter(
        l => l.source === nodeId || l.target === nodeId
      );
      const linksDiv = document.createElement('div');
      linksDiv.className = 'detail-prop';
      linksDiv.style.marginTop = '0.75rem';
      const linksStrong = document.createElement('strong');
      linksStrong.textContent = 'Links (' + connectedLinks.length + '):';
      linksDiv.appendChild(linksStrong);
      detailsContainer.appendChild(linksDiv);

      if (connectedLinks.length > 0) {{
        const ul = document.createElement('ul');
        ul.style.paddingLeft = '1.2rem';
        ul.style.marginTop = '0.25rem';
        ul.style.fontSize = '0.85rem';
        connectedLinks.forEach(l => {{
          const li = document.createElement('li');
          const other = l.source === nodeId ? l.target : l.source;
          const ltype = l.link_type || 'ethernet';
          const lbl = l.label ? ' (' + l.label + ')' : '';
          li.textContent = ltype + ' -> ' + other + lbl;
          ul.appendChild(li);
        }});
        detailsContainer.appendChild(ul);
      }}
    }}

    document.querySelectorAll('.topology-node').forEach(nodeEl => {{
      nodeEl.addEventListener('click', function() {{
        const nid = this.getAttribute('data-id');
        if (nid) selectNode(nid);
      }});
      nodeEl.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter' || e.key === ' ') {{
          e.preventDefault();
          const nid = this.getAttribute('data-id');
          if (nid) selectNode(nid);
        }}
      }});
    }});

    document.querySelectorAll('.table-node-row').forEach(rowEl => {{
      rowEl.addEventListener('click', function() {{
        const nid = this.getAttribute('data-id');
        if (nid) selectNode(nid);
      }});
    }});

    document.getElementById('search-input')?.addEventListener('input', function(e) {{
      const query = (e.target.value || '').trim().toLowerCase();

      document.querySelectorAll('.topology-node').forEach(nodeEl => {{
        const nid = (nodeEl.getAttribute('data-id') || '').toLowerCase();
        const nlbl = (nodeEl.getAttribute('data-label') || '').toLowerCase();
        const nrole = (nodeEl.getAttribute('data-role') || '').toLowerCase();
        const nvendor = (nodeEl.getAttribute('data-vendor') || '').toLowerCase();
        const nsite = (nodeEl.getAttribute('data-site') || '').toLowerCase();

        const match = !query ||
          nid.includes(query) ||
          nlbl.includes(query) ||
          nrole.includes(query) ||
          nvendor.includes(query) ||
          nsite.includes(query);
        if (match) {{
          nodeEl.classList.remove('dimmed');
        }} else {{
          nodeEl.classList.add('dimmed');
        }}
      }});

      document.querySelectorAll('.topology-link').forEach(linkEl => {{
        const src = (linkEl.getAttribute('data-source') || '').toLowerCase();
        const dst = (linkEl.getAttribute('data-target') || '').toLowerCase();
        const type = (linkEl.getAttribute('data-type') || '').toLowerCase();

        const match = !query || src.includes(query) || dst.includes(query) || type.includes(query);
        if (match) {{
          linkEl.classList.remove('dimmed');
        }} else {{
          linkEl.classList.add('dimmed');
        }}
      }});

      document.querySelectorAll('.table-node-row').forEach(rowEl => {{
        const text = rowEl.textContent.toLowerCase();
        if (!query || text.includes(query)) {{
          rowEl.style.display = '';
        }} else {{
          rowEl.style.display = 'none';
        }}
      }});
    }});

    document.getElementById('btn-download-svg')?.addEventListener('click', function() {{
      const svgEl = document.getElementById('topology-svg');
      if (!svgEl) return;
      const clone = svgEl.cloneNode(true);
      const serializer = new XMLSerializer();
      let source = serializer.serializeToString(clone);
      if (!source.match(/^<svg[^>]+xmlns="http\\:\\/\\/www\\.w3\\.org\\/2000\\/svg"/)) {{
        source = source.replace(/^<svg/, '<svg xmlns="http://www.w3.org/2000/svg"');
      }}
      const blob = new Blob([source], {{ type: 'image/svg+xml;charset=utf-8' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'topology.svg';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }});
  </script>
</body>
</html>
"""
