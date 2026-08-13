"""Graphviz DOT (+ optional rendered image) exporter."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hpe_networking_mcp.mcp_servers.design_lib.icons import icon_path_for
from hpe_networking_mcp.mcp_servers.design_lib.model import DiagramModel, ROLE_LAYER

_ROLE_SHAPE: dict[str, str] = {
    "cloud": "cloud",
    "internet": "cloud",
    "firewall": "box",
    "router": "box",
    "core_switch": "box3d",
    "agg_switch": "box3d",
    "access_switch": "box3d",
    "gateway": "component",
    "campus_ap": "hexagon",
    "mist_ap": "hexagon",
    "clearpass": "cylinder",
    "controller": "component",
    "server": "cylinder",
    "client": "ellipse",
    "generic": "box",
}

_LINK_STYLE: dict[str, str] = {
    "ethernet": 'color="#555555", penwidth=1.5',
    "trunk": 'color="#111111", penwidth=2.5',
    "wireless": 'color="#6C8EBF", penwidth=1.5, style=dashed',
    "wan": 'color="#B85450", penwidth=1.5, style=dashed',
    "logical": 'color="#9673A6", penwidth=1.0, style=dotted',
}


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# Fixed icon box in inches — without this, Graphviz uses native pixel size and
# 2k–4k product photos dominate the canvas.
_ICON_WIDTH_IN = 0.55
_ICON_HEIGHT_IN = 0.55


def build_dot(model: DiagramModel, *, rankdir: str = "TB") -> str:
    lines = [
        "digraph network {",
        f'  graph [rankdir={rankdir}, bgcolor="white", fontname="Helvetica", '
        f'nodesep=0.45, ranksep=0.65, pad=0.2, splines=true];',
        '  node [fontname="Helvetica", fontsize=9, margin="0.06,0.04"];',
        '  edge [fontname="Helvetica", fontsize=8];',
        f'  labelloc="t"; label="{_esc(model.title)}";',
    ]

    # invisible rank constraints by role layer
    by_layer: dict[int, list[str]] = {}
    for node in model.nodes:
        by_layer.setdefault(ROLE_LAYER.get(node.role, 4), []).append(node.id)

    for node in model.nodes:
        shape = _ROLE_SHAPE.get(node.role, "box")
        label = _esc(node.label)
        if node.mgmt_ip:
            label = f"{label}\\n{_esc(node.mgmt_ip)}"
        if node.vendor and node.vendor != "generic":
            label = f"{label}\\n[{_esc(node.vendor)}]"
        attrs = [
            f'label="{label}"',
            f"shape={shape}",
            'style="filled,rounded"',
            'fillcolor="#F7F7F7"',
            'color="#666666"',
            "width=1.15",
            "height=0.55",
            "fixedsize=true",
        ]
        icon = icon_path_for(node.vendor, node.role)
        if icon is not None:
            # Compact fixed-size image node; label under the glyph.
            attrs = [
                f'label="{label}"',
                "shape=box",
                f'image="{_esc(str(icon))}"',
                "imagescale=true",
                "fixedsize=true",
                f"width={_ICON_WIDTH_IN}",
                f"height={_ICON_HEIGHT_IN}",
                'labelloc="b"',
                'imagepos="tc"',
                'style="filled"',
                'fillcolor="white"',
                'color="#CCCCCC"',
                'penwidth=0.5',
            ]
        lines.append(f'  "{_esc(node.id)}" [{", ".join(attrs)}];')

    for layer in sorted(by_layer):
        ids = " ".join(f'"{_esc(i)}"' for i in by_layer[layer])
        lines.append(f"  {{ rank=same; {ids}; }}")

    for link in model.links:
        style = _LINK_STYLE.get(link.link_type, _LINK_STYLE["ethernet"])
        label = link.label or link.bandwidth or ""
        label_attr = f', label="{_esc(label)}"' if label else ""
        lines.append(
            f'  "{_esc(link.source)}" -> "{_esc(link.target)}" '
            f"[dir=none, {style}{label_attr}];"
        )

    for note in model.notes[:5]:
        lines.append(f'  // note: {_esc(note)}')

    lines.append("}")
    return "\n".join(lines) + "\n"


def export_graphviz(
    model: DiagramModel,
    *,
    rankdir: str = "TB",
    render_format: str | None = None,
) -> dict[str, Any]:
    """Build DOT; optionally render via system ``dot`` to svg/png/pdf bytes path info.

    ``render_format``: None | svg | png | pdf. Rendering requires Graphviz ``dot``
    on PATH; when missing, DOT is still returned and render is skipped.
    """
    if rankdir not in {"TB", "LR", "BT", "RL"}:
        rankdir = "TB"
    dot = build_dot(model, rankdir=rankdir)
    result: dict[str, Any] = {
        "format": "graphviz",
        "filename_ext": ".dot",
        "content": dot,
        "content_type": "text/vnd.graphviz",
        "title": model.title,
        "node_count": len(model.nodes),
        "link_count": len(model.links),
        "dot_available": bool(shutil.which("dot")),
        "rendered": None,
    }

    fmt = (render_format or "").strip().lower() or None
    if fmt not in {None, "svg", "png", "pdf"}:
        result["render_error"] = f"unsupported render_format {render_format!r}; use svg|png|pdf"
        return result

    if fmt is None:
        return result

    if not result["dot_available"]:
        result["render_error"] = (
            "Graphviz 'dot' not found on PATH; install graphviz to render images. "
            "DOT source is still available in content."
        )
        return result

    with tempfile.TemporaryDirectory(prefix="hpe-networking-mcp-dot-") as tmp:
        src = Path(tmp) / "graph.dot"
        out = Path(tmp) / f"graph.{fmt}"
        src.write_text(dot, encoding="utf-8")
        try:
            subprocess.run(
                ["dot", f"-T{fmt}", str(src), "-o", str(out)],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            result["render_error"] = f"dot render failed: {exc}"
            return result
        data = out.read_bytes()
        # Bound inline payload (~1.5MB)
        if len(data) > 1_500_000:
            result["render_error"] = f"rendered {fmt} exceeds 1.5MB bound ({len(data)} bytes)"
            return result
        if fmt == "svg":
            result["rendered"] = {
                "format": "svg",
                "content_type": "image/svg+xml",
                "content": data.decode("utf-8", errors="replace"),
                "filename_ext": ".svg",
            }
        else:
            import base64

            result["rendered"] = {
                "format": fmt,
                "content_type": "image/png" if fmt == "png" else "application/pdf",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "byte_length": len(data),
                "filename_ext": f".{fmt}",
            }
    return result
