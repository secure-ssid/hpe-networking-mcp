"""Unit tests for the optional design / diagram backend."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from hpe_networking_mcp.mcp_servers import design as design_mod
from hpe_networking_mcp.mcp_servers.design_lib import model as design_model
from hpe_networking_mcp.mcp_servers.tool_router import (
    _OPTIONAL_BACKENDS,
    _TOOLSET_BACKENDS,
    _build_backends,
)

SAMPLE_MODEL = {
    "title": "Branch lab",
    "nodes": [
        {"id": "core", "label": "Core-SW1", "role": "core_switch", "vendor": "aruba"},
        {"id": "ap1", "label": "AP-01", "role": "campus_ap", "vendor": "aruba"},
        {"id": "cppm", "label": "ClearPass", "role": "clearpass", "vendor": "clearpass"},
    ],
    "links": [
        {"source": "core", "target": "ap1", "link_type": "ethernet", "bandwidth": "2.5G"},
        {"source": "core", "target": "cppm", "link_type": "logical"},
    ],
    "groups": [{"id": "branch", "label": "Branch A", "members": ["core", "ap1", "cppm"]}],
}


def _call(tool_fn, **kwargs):
    target = getattr(tool_fn, "fn", tool_fn)
    return target(**kwargs)


def test_optional_backend_registration(monkeypatch):
    assert _OPTIONAL_BACKENDS["design"] == ("design-core", "hpe_networking_mcp.mcp_servers.design")
    assert _TOOLSET_BACKENDS["design"] == {"design-core"}
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "design")
    backends = _build_backends()
    assert backends.get("design-core") == "hpe_networking_mcp.mcp_servers.design"


def test_validate_and_drawio_export(tmp_path, monkeypatch):
    monkeypatch.setattr(
        design_mod,
        "write_text_artifact",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no save")),
    )
    out = _call(design_mod.validate_diagram_model, model=SAMPLE_MODEL)
    assert out.get("ok") is True or out.get("data", {}).get("ok") is True or "model" in str(out)

    result = _call(design_mod.drawio_network_design_diagram, model=SAMPLE_MODEL, save=False)
    assert result["ok"] is True
    xml = result["export"]["content"]
    assert xml.startswith("<?xml")
    assert "mxfile" in xml
    assert "Core-SW1" in xml
    assert result["saved"] is False


def test_drawio_save_sandbox(tmp_path, monkeypatch):
    from hpe_networking_mcp.mcp_servers.design_lib import files as design_files

    monkeypatch.setattr(design_files, "DIAGRAM_OUT", tmp_path)
    monkeypatch.setattr(design_mod, "write_text_artifact", design_files.write_text_artifact)
    monkeypatch.setattr(design_mod, "write_json_artifact", design_files.write_json_artifact)
    monkeypatch.setattr(design_mod, "write_bytes_artifact", design_files.write_bytes_artifact)

    result = _call(
        design_mod.drawio_network_design_diagram,
        model=SAMPLE_MODEL,
        save=True,
        filename_stem="unit_branch",
    )
    assert result["ok"] is True
    assert result["saved"] is True
    paths = [Path(w["path"]) for w in result["written"]]
    assert paths
    assert paths[0].exists()
    assert paths[0].suffix == ".drawio"
    assert paths[0].parent == tmp_path


def test_graphviz_dot_and_optional_render():
    result = _call(design_mod.export_graphviz_topology, model=SAMPLE_MODEL, save=False)
    assert result["ok"] is True
    dot = result["export"]["content"]
    assert "digraph network" in dot
    assert "Core-SW1" in dot


def test_next_ui_export():
    result = _call(design_mod.export_next_ui_topology, model=SAMPLE_MODEL, save=False)
    assert result["ok"] is True
    topo = result["export"]["content"]
    assert len(topo["nodes"]) == 3
    assert len(topo["links"]) == 2
    preview = result["export"]["preview_html"]
    assert "<svg" in preview
    assert "Branch lab" in preview
    assert "Operator-supplied review artifact" in preview


def test_next_ui_save_writes_complete_artifact(tmp_path, monkeypatch):
    from hpe_networking_mcp.mcp_servers.design_lib import files as design_files

    monkeypatch.setattr(design_files, "DIAGRAM_OUT", tmp_path)
    monkeypatch.setattr(design_mod, "write_text_artifact", design_files.write_text_artifact)
    monkeypatch.setattr(design_mod, "write_json_artifact", design_files.write_json_artifact)
    monkeypatch.setattr(design_mod, "write_bytes_artifact", design_files.write_bytes_artifact)

    result = _call(
        design_mod.export_next_ui_topology,
        model=SAMPLE_MODEL,
        save=True,
        filename_stem="unit_next",
    )
    assert result["ok"] is True
    assert result["saved"] is True
    written_paths = [Path(w["path"]) for w in result["written"]]
    assert len(written_paths) >= 2
    exts = {p.suffix for p in written_paths}
    assert ".json" in exts or ".next" in str(written_paths)
    assert ".html" in exts
    for p in written_paths:
        assert p.exists()
        assert p.parent == tmp_path
        assert p.stat().st_size > 0


def test_next_ui_hostile_labels_stay_inert():
    hostile_model = {
        "title": "Hostile <script>alert('xss1')</script>",
        "nodes": [
            {
                "id": "n1",
                "label": "<script>alert('xss2')</script>",
                "role": "core_switch",
                "vendor": "aruba",
                "site": '"><img src=x onerror=alert(1)>',
            },
            {
                "id": "n2",
                "label": "</script><script>alert('xss3')",
                "role": "access_switch",
                "vendor": "hpe",
            },
        ],
        "links": [
            {
                "source": "n1",
                "target": "n2",
                "label": "<iframe src=javascript:alert('xss4')>",
            }
        ],
        "groups": [
            {
                "id": "g1",
                "label": "<svg/onload=alert('xss5')>",
                "members": ["n1", "n2"],
            }
        ],
    }
    result = _call(design_mod.export_next_ui_topology, model=hostile_model, save=False)
    assert result["ok"] is True
    html = result["export"]["preview_html"]

    # Verify HTML/XML text is escaped and unescaped script tags don't appear in body HTML
    assert "<script>alert('xss1')</script>" not in html
    assert (
        "&lt;script&gt;alert(&#27;xss1&#27;)&lt;/script&gt;" in html
        or "&lt;script&gt;alert('xss1')&lt;/script&gt;" in html
        or "&lt;script&gt;" in html
    )
    assert "<iframe" not in html
    assert "<svg/onload" not in html

    # Verify JSON inside <script> doesn't break script block boundaries
    assert "</script><script>alert('xss3')" not in html


def test_next_ui_deterministic_export():
    res1 = _call(design_mod.export_next_ui_topology, model=SAMPLE_MODEL, save=False)
    res2 = _call(design_mod.export_next_ui_topology, model=SAMPLE_MODEL, save=False)
    assert res1["export"]["preview_html"] == res2["export"]["preview_html"]
    assert res1["export"]["content"] == res2["export"]["content"]


def test_next_ui_100_nodes_200_links_fixture_preserves_topology_within_response_contract():
    nodes = [
        {
            "id": f"node_{i}",
            "label": f"Switch-{i}",
            "role": "access_switch" if i > 5 else "core_switch",
            "vendor": "aruba" if i % 2 == 0 else "juniper",
            "site": "Site-A" if i < 50 else "Site-B",
        }
        for i in range(100)
    ]
    links = [
        {
            "source": f"node_{i % 100}",
            "target": f"node_{(i + 1) % 100}",
            "link_type": "ethernet" if i % 2 == 0 else "trunk",
            "label": f"10G-link-{i}",
        }
        for i in range(200)
    ]
    groups = [
        {"id": "g_a", "label": "Group A", "members": [f"node_{i}" for i in range(50)]},
        {"id": "g_b", "label": "Group B", "members": [f"node_{i}" for i in range(50, 100)]},
    ]

    fixture_model = {
        "title": "100-Node Scale Test Topology",
        "nodes": nodes,
        "links": links,
        "groups": groups,
    }

    result = _call(design_mod.export_next_ui_topology, model=fixture_model, save=False)
    assert result["ok"] is True
    assert result["export"]["node_count"] == 100
    assert result["export"]["link_count"] == 200
    assert len(result["export"]["content"]["nodes"]) == 100
    assert len(result["export"]["content"]["links"]) == 200

    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) <= 180_000, f"response length {len(encoded)} exceeds budget 180_000"


def test_topology_conversion():
    topo = {
        "nodes": [
            {"id": "SW1", "name": "sw-core", "type": "SWITCH"},
            {"serial": "CN123", "hostname": "ap-lobby", "device_type": "AP"},
        ],
        "links": [{"source": "SW1", "target": "CN123", "type": "ethernet"}],
    }
    result = _call(
        design_mod.drawio_network_design_diagram,
        topology=topo,
        title="From Central",
        site_id="site-1",
        save=False,
    )
    assert result["ok"] is True
    assert "sw-core" in result["export"]["content"]


def test_invalid_model():
    result = _call(
        design_mod.drawio_network_design_diagram,
        model={"title": "x", "nodes": [], "links": []},
        save=False,
    )
    assert result["ok"] is False


def test_list_icons_and_roles():
    icons = _call(design_mod.list_diagram_icons)
    payload = icons if "icon_count" in icons else icons.get("data", icons)
    # response_payload may wrap
    if "icon_count" not in payload and isinstance(icons, dict):
        # flatten common envelope
        payload = icons
        for key in ("result", "data", "payload"):
            if isinstance(icons.get(key), dict) and "icon_count" in icons[key]:
                payload = icons[key]
    assert payload.get("icon_count", 0) >= 1 or "external_sources" in payload

    roles = _call(design_mod.list_diagram_roles_and_vendors)
    text = json.dumps(roles)
    assert "drawio_network_design_diagram" in text
    assert "core_switch" in text


def test_known_roles_cover_sample():
    for node in SAMPLE_MODEL["nodes"]:
        assert node["role"] in design_model.KNOWN_ROLES


FLOW_MODEL = {
    "title": "Quickstart",
    "nodes": [
        {"id": "clone", "label": "1. Clone\nthe repo"},
        {"id": "check", "label": "Ready?", "extra": {"shape": "decision"}},
        {"id": "done", "label": "Call a tool", "extra": {"shape": "terminal"}},
    ],
    "links": [
        {"source": "clone", "target": "check"},
        {"source": "check", "target": "done", "label": "yes"},
    ],
}


def test_flow_export_is_directed_and_machine_independent():
    """Flow DOT must be portable: topology exports embed absolute, gitignored icon paths."""
    out = _call(design_mod.export_flow_diagram, model=FLOW_MODEL, render_format=None)
    dot = out["export"]["content"]

    assert "digraph" in dot and "->" in dot
    assert "image=" not in dot, "flow diagrams must not reference machine-local icon files"
    assert "shape=diamond" in dot and "shape=oval" in dot
    assert 'label="yes"' in dot


def test_flow_export_rejects_unknown_shape():
    """A typo must fail loudly instead of silently degrading to a plain box."""
    broken = {
        "title": "Broken",
        "nodes": [{"id": "a", "label": "A", "extra": {"shape": "hexagonal-prism"}}],
        "links": [],
    }
    out = _call(design_mod.export_flow_diagram, model=broken, render_format=None)

    assert out["ok"] is False
    assert "hexagonal-prism" in out["error"]


def test_topology_export_is_unchanged_by_flow_mode():
    """Flow mode is additive; the network topology exporter keeps its own defaults."""
    topology = _call(design_mod.export_graphviz_topology, model=SAMPLE_MODEL)
    dot = topology["export"]["content"]

    assert dot.startswith("digraph network")
    assert "rank=same" in dot


def test_next_ui_skipped_layer_link_routing_and_group_header_spacing():
    """Skipped-layer links get a route and groups reserve a separate header."""
    model = {
        "title": "Skipped Layer Network",
        "nodes": [
            {"id": "core", "label": "Core Switch", "role": "core_switch", "vendor": "aruba"},
            {"id": "identity", "label": "ClearPass", "role": "clearpass", "vendor": "clearpass"},
            {"id": "access", "label": "Access Switch", "role": "access_switch", "vendor": "aruba"},
        ],
        "links": [
            {"source": "core", "target": "identity", "link_type": "logical"},
            {"source": "core", "target": "access", "link_type": "ethernet"},
        ],
        "groups": [{"id": "site_g", "label": "Site Group", "members": ["identity", "access"]}],
    }
    result = _call(design_mod.export_next_ui_topology, model=model, save=False)
    assert result["ok"] is True
    html = result["export"]["preview_html"]
    assert '<path d="M' in html
    assert " Q " in html
    assert "Site Group" in html
    assert "group-box" in html


def test_next_ui_standalone_svg_style_embedding():
    """Generated SVG embeds CSS styles in <defs> for standalone portability."""
    result = _call(design_mod.export_next_ui_topology, model=SAMPLE_MODEL, save=False)
    assert result["ok"] is True
    html = result["export"]["preview_html"]
    svg = ET.fromstring(html[html.index("<svg") : html.index("</svg>") + 6])
    style = svg.find("{*}defs/{*}style")
    assert style is not None and style.text
    assert ".group-box" in style.text
    assert ".node-body" in style.text
    assert ".topology-link" in style.text


def test_next_ui_endpoint_metadata_and_keyboard_contract():
    result = _call(design_mod.export_next_ui_topology, model=SAMPLE_MODEL, save=False)
    html = result["export"]["preview_html"]
    svg = ET.fromstring(html[html.index("<svg") : html.index("</svg>") + 6])
    links = [
        element for element in svg.iter() if "topology-link" in element.get("class", "").split()
    ]
    assert [(link.get("data-source"), link.get("data-target")) for link in links] == [
        (link["source"], link["target"]) for link in SAMPLE_MODEL["links"]
    ]
    nodes = [
        element for element in svg.iter() if "topology-node" in element.get("class", "").split()
    ]
    assert len(nodes) == len(SAMPLE_MODEL["nodes"])
    for node in nodes:
        assert node.get("tabindex") == "0"
        assert node.get("role") == "button"
        assert node.get("aria-label")
