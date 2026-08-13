from __future__ import annotations

import os
import sys

from hpe_networking_mcp.pipeline import project_facts
from scripts import ingest_tools


def test_complete_catalog_pins_environment_and_selects_all(monkeypatch):
    for name in project_facts.CATALOG_ENV:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "mist")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_tools.py", "--complete-catalog"],
    )
    captured: dict[str, str | None] = {}

    def fake_main(products: str | None = None) -> int:
        captured["products"] = products
        return 0

    monkeypatch.setattr(ingest_tools, "main_lancedb", fake_main)

    assert ingest_tools.main() == 0
    assert captured["products"] == "all"
    assert all(
        os.environ[name] == value
        for name, value in project_facts.CATALOG_ENV.items()
    )
    assert "HPE_MCP_PRODUCTS" not in os.environ
