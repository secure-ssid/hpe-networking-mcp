"""Unit tests for Graphviz ``dot`` resolution in the design exporter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hpe_networking_mcp.mcp_servers.design_lib import graphviz_export
from hpe_networking_mcp.mcp_servers.design_lib.model import parse_model

MODEL = parse_model(
    {
        "title": "Lab",
        "nodes": [{"id": "core", "label": "Core-SW1", "role": "core_switch"}],
        "links": [],
    }
)


def _fake_run(captured: dict):
    def run(argv, **kwargs):
        captured["argv"] = argv
        out = Path(argv[argv.index("-o") + 1])
        out.write_bytes(b"<svg></svg>")
        return subprocess.CompletedProcess(argv, 0)

    return run


def test_render_invokes_resolved_absolute_dot_path_not_bare_name(tmp_path, monkeypatch):
    """``subprocess.run`` must receive the fully-resolved ``dot`` path.

    Spawning the bare program name re-resolves through PATH at exec time (on
    Windows, the working directory first), so a writable CWD can substitute a
    different binary between the availability check and the render.
    """
    fake_dot = tmp_path / "dot.exe"
    fake_dot.write_bytes(b"MZ")
    monkeypatch.setattr(
        graphviz_export.shutil, "which", lambda name: str(fake_dot) if name == "dot" else None
    )
    captured: dict = {}
    monkeypatch.setattr(graphviz_export.subprocess, "run", _fake_run(captured))

    result = graphviz_export.export_graphviz(MODEL, render_format="svg")

    assert captured["argv"][0] == str(fake_dot.resolve())
    assert Path(captured["argv"][0]).is_absolute()
    assert result["rendered"]["format"] == "svg"


def test_unresolvable_which_result_is_treated_as_dot_missing(tmp_path, monkeypatch):
    """A ``which()`` hit that fails validation counts as ``dot`` absent."""
    ghost = tmp_path / "no-such-dot"
    monkeypatch.setattr(
        graphviz_export.shutil, "which", lambda name: str(ghost) if name == "dot" else None
    )

    result = graphviz_export.export_graphviz(MODEL, render_format="svg")

    assert result["dot_available"] is False
    assert result["rendered"] is None
    assert "not found" in result["render_error"]
