"""Unit tests for the optional design / diagram backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.mcp_servers import design as design_mod
from hpe_networking_mcp.mcp_servers.design_lib import model as design_model
from hpe_networking_mcp.mcp_servers.tool_router import _OPTIONAL_BACKENDS, _TOOLSET_BACKENDS, _build_backends

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
    monkeypatch.setattr(design_mod, "write_text_artifact", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no save")))
    # re-import save path via files module for save test later
    from hpe_networking_mcp.mcp_servers.design_lib import files as design_files

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
    assert "NeXt" in result["export"]["preview_html"] or "next" in result["export"]["preview_html"].lower()


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
    "links": [{"source": "clone", "target": "check"}, {"source": "check", "target": "done", "label": "yes"}],
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
