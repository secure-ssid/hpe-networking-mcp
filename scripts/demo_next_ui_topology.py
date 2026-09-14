#!/usr/bin/env python3
"""Generate a synthetic topology preview HTML artifact for browser verification.

Uses fake device identifiers and mock site metadata (no real tenant data).
Outputs both .next.json and .html preview files under outputs/diagrams/.

Usage:
    uv run python scripts/demo_next_ui_topology.py
"""

from __future__ import annotations

import json
from pathlib import Path

from hpe_networking_mcp.mcp_servers.design import export_next_ui_topology

SYNTHETIC_TOPOLOGY = {
    "title": "Synthetic Campus & Branch Topology",
    "nodes": [
        {
            "id": "fw-edge-01",
            "label": "FW-EDGE-01",
            "role": "firewall",
            "vendor": "juniper",
            "site": "Site-HQ",
            "mgmt_ip": "192.0.2.1",
            "serial": "MOCK-FW-001",
        },
        {
            "id": "core-sw-01",
            "label": "CORE-SW-01",
            "role": "core_switch",
            "vendor": "aruba",
            "site": "Site-HQ",
            "mgmt_ip": "192.0.2.10",
            "serial": "MOCK-CORE-001",
        },
        {
            "id": "agg-sw-01",
            "label": "AGG-SW-01",
            "role": "agg_switch",
            "vendor": "aruba",
            "site": "Site-HQ",
            "mgmt_ip": "192.0.2.20",
            "serial": "MOCK-AGG-001",
        },
        {
            "id": "access-ap-01",
            "label": "AP-LOBBY-01",
            "role": "campus_ap",
            "vendor": "aruba",
            "site": "Site-HQ",
            "mgmt_ip": "192.0.2.50",
            "serial": "MOCK-AP-001",
        },
        {
            "id": "clearpass-01",
            "label": "CPPM-AUTH-01",
            "role": "clearpass",
            "vendor": "clearpass",
            "site": "Site-HQ",
            "mgmt_ip": "192.0.2.100",
            "serial": "MOCK-CPPM-001",
        },
    ],
    "links": [
        {
            "source": "fw-edge-01",
            "target": "core-sw-01",
            "link_type": "wan",
            "label": "WAN-Uplink 10G",
        },
        {
            "source": "core-sw-01",
            "target": "agg-sw-01",
            "link_type": "trunk",
            "label": "Trunk 40G",
        },
        {
            "source": "agg-sw-01",
            "target": "access-ap-01",
            "link_type": "ethernet",
            "bandwidth": "2.5G PoE+",
        },
        {
            "source": "core-sw-01",
            "target": "clearpass-01",
            "link_type": "logical",
            "label": "RADIUS / TACACS+",
        },
    ],
    "groups": [
        {
            "id": "hq_core",
            "label": "HQ Core Infrastructure",
            "members": ["fw-edge-01", "core-sw-01", "clearpass-01"],
        },
        {
            "id": "hq_access",
            "label": "HQ Access Layer",
            "members": ["agg-sw-01", "access-ap-01"],
        },
    ],
    "notes": [
        "Synthetic demo topology generated for browser verification.",
        "Operator-supplied review artifact — not validated live network state.",
    ],
}


def main() -> int:
    result = export_next_ui_topology(
        model=SYNTHETIC_TOPOLOGY,
        save=True,
        filename_stem="demo_synthetic_topology",
    )
    if not result.get("ok"):
        print(f"Export failed: {result.get('error')}")
        return 1

    written = result.get("written", [])
    print("## Export Succeeded")
    print(json.dumps({"saved": result.get("saved"), "written": written}, indent=2))

    html_path = None
    for w in written:
        p = Path(w["path"])
        if p.suffix == ".html":
            html_path = p
            break

    if html_path:
        print(f"\nTo inspect the visual topology in a browser, open:\nfile://{html_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
