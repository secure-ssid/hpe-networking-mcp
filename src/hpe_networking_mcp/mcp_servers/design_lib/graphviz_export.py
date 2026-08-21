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


# --- Flow diagrams ---------------------------------------------------------
# Topology drawings answer "what is connected to what": undirected links,
# vendor icons, and rank bands derived from network role. Documentation
# flowcharts answer "what happens next", so they need the opposite defaults --
# arrowheads, labels that size their own node, and no role banding. They must
# also stay reproducible on any machine: an icon path is absolute and private
# (``resources/diagram_icons/`` is gitignored), so a committed artifact that
# referenced one would render only on the machine that drew it.

#: Node shapes a flow model may request via ``extra.shape``.
_FLOW_SHAPES: dict[str, str] = {
    "box": "box",
    "decision": "diamond",
    "store": "cylinder",
    "terminal": "oval",
}

_FLOW_FILL = "#ECECEC"
_FLOW_STROKE = "#999999"
_FLOW_TEXT = "#333333"
_FLOW_EDGE = "#666666"


def _flow_label(text: str) -> str:
    r"""Escape ``text`` for DOT, turning real newlines into centered ``\n`` breaks."""
    return _esc(text).replace("\n", "\\n")


def build_flow_dot(model: DiagramModel, *, rankdir: str = "LR") -> str:
    """DOT for a documentation flowchart: directed, auto-sized, icon-free.

    Args:
        model: Nodes carry the step label; ``extra.shape`` selects a shape from
            :data:`_FLOW_SHAPES`. Links are drawn in order with optional labels.
        rankdir: Graphviz direction; ``LR`` keeps a linear journey a wide band
            rather than a tall ribbon.

    The model title is deliberately not drawn: these render inside a
    ``<figure>`` that already carries a caption, and a duplicated heading
    inside the image cannot be selected, translated, or restyled by the page.
    """
    # An opaque canvas, not "transparent": edge labels ("Yes"/"No") sit on the
    # background rather than inside a filled node, so on GitHub's dark theme a
    # transparent canvas renders them dark-grey-on-near-black. A static SVG
    # cannot restyle itself per theme once GitHub embeds it through <img>, so
    # the drawing carries its own background and reads identically in both.
    lines = [
        "digraph flow {",
        f'  graph [rankdir={rankdir}, bgcolor="white", fontname="Helvetica", '
        "nodesep=0.35, ranksep=0.45, pad=0.15, splines=true];",
        f'  node [fontname="Helvetica", fontsize=11, fontcolor="{_FLOW_TEXT}", '
        f'shape=box, style="filled,rounded", fillcolor="{_FLOW_FILL}", '
        f'color="{_FLOW_STROKE}", penwidth=1, margin="0.18,0.10"];',
        f'  edge [fontname="Helvetica", fontsize=9, fontcolor="{_FLOW_TEXT}", '
        f'color="{_FLOW_EDGE}", penwidth=1.1, arrowsize=0.7];',
    ]

    for node in model.nodes:
        requested = str(node.extra.get("shape", "box")).strip().lower()
        if requested not in _FLOW_SHAPES:
            raise ValueError(
                f"node {node.id!r}: unknown flow shape {requested!r}; "
                f"expected one of {sorted(_FLOW_SHAPES)}"
            )
        shape = _FLOW_SHAPES[requested]
        attrs = [f'label="{_flow_label(node.label)}"']
        if shape != "box":
            attrs.append(f"shape={shape}")
        if shape == "diamond":
            # A diamond's label box is inscribed, so default margins wrap the
            # text into a very tall lozenge; flatten it back out.
            attrs.append('margin="0.02,0.02"')
        lines.append(f'  "{_esc(node.id)}" [{", ".join(attrs)}];')

    for link in model.links:
        label = link.label or link.bandwidth or ""
        edge = f'  "{_esc(link.source)}" -> "{_esc(link.target)}"'
        if label:
            edge += f' [label="{_flow_label(label)}"]'
        lines.append(f"{edge};")

    lines.append("}")
    return "\n".join(lines) + "\n"


def export_graphviz(
    model: DiagramModel,
    *,
    rankdir: str = "TB",
    render_format: str | None = None,
    flow: bool = False,
) -> dict[str, Any]:
    """Build DOT; optionally render via system ``dot`` to svg/png/pdf bytes path info.

    ``render_format``: None | svg | png | pdf. Rendering requires Graphviz ``dot``
    on PATH; when missing, DOT is still returned and render is skipped.

    ``flow``: draw a documentation flowchart (:func:`build_flow_dot`) instead of
    a network topology -- directed edges, self-sizing labels, and no icons.
    """
    if rankdir not in {"TB", "LR", "BT", "RL"}:
        rankdir = "TB"
    dot = build_flow_dot(model, rankdir=rankdir) if flow else build_dot(model, rankdir=rankdir)
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
