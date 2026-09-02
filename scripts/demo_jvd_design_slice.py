#!/usr/bin/env python3
"""Vertical slice: requirements -> SKU -> JVD -> BOM/topology review bundle.

This script is the first end-to-end demonstration of the structured
Juniper/Mist workflow, run against the *real* committed catalog and JVD
indexes (not test fixtures). It exists to prove the full chain works
together and to make the current real-data gap between the hardware
catalog (currently Campus/Branch EX-series access switches) and the JVD
structured index (currently Data Center/Enterprise WAN/Optical/Security/
Service Provider designs) visible and documented, rather than silently
papered over.

Run it after building both local indexes:

    uv run python scripts/build_hardware_catalog.py
    uv run python scripts/build_jvd_index.py
    uv run python scripts/demo_jvd_design_slice.py

It never renders or pushes device configuration -- see the JVD design/build
boundary in docs/juniper-mist-jvd.md.
"""

from __future__ import annotations

import json

from hpe_networking_mcp.pipeline.artifact_validation import validate_for_review
from hpe_networking_mcp.pipeline.clients import hardware_catalog, jvd_catalog
from hpe_networking_mcp.pipeline.design_bundle import build_design_bundle


def main() -> int:
    # Step 1: resolve requirement -> exact SKU via the deterministic catalog
    # (never guess; ambiguous requirements return match_type=candidate and
    # must be re-asked with more detail before proceeding).
    sku_lookup = hardware_catalog.search("EX4400 24 port PoE access switch", vendor="juniper")
    print("## Step 1: SKU lookup")
    print(json.dumps({k: sku_lookup[k] for k in ("ok", "match_type")}, indent=2))
    if sku_lookup.get("match_type") != "exact_sku":
        exact_sku = "EX4400-24P"  # requirement was ambiguous; pin one for this demo
        print(f"(ambiguous candidate list; pinning {exact_sku!r} for this demo)\n")
    else:
        exact_sku = sku_lookup["results"][0]["sku"]

    # Step 2: find a candidate validated design by area/use case.
    jvd_lookup = jvd_catalog.search_designs("EVPN VXLAN data center fabric")
    print("## Step 2: JVD design lookup")
    print(json.dumps({k: jvd_lookup[k] for k in ("ok", "match_type")}, indent=2))
    design_id = jvd_lookup["results"][0]["id"] if jvd_lookup.get("ok") else None
    print()

    # Step 3: compose the review bundle -- BOM + topology + JVD reference,
    # with every field explicitly labeled official/operator_input/derived/unknown.
    bundle = build_design_bundle(
        title="EX4400-24P access pilot (vertical slice demo)",
        line_items=[{"role": "access_switch", "sku": exact_sku, "quantity": 2}],
        topology={
            "title": "EX4400-24P pilot",
            "nodes": [
                {"id": "acc1", "label": exact_sku, "role": "access_switch", "vendor": "juniper"},
                {"id": "acc2", "label": exact_sku, "role": "access_switch", "vendor": "juniper"},
            ],
            "links": [{"source": "acc1", "target": "acc2", "link_type": "trunk"}],
        },
        jvd_design_id=design_id,
    )
    print("## Step 3: design bundle")
    print(json.dumps(bundle, indent=2, sort_keys=True))

    # Step 4: fail-closed review gate -- classify problems as blocking
    # (must not present as ready) vs advisory (worth a reviewer's attention).
    review = validate_for_review(bundle)
    print("\n## Step 4: review gate")
    print(json.dumps(review, indent=2, sort_keys=True))

    # This demo's real, honest finding: today's Juniper/Mist catalog seed and
    # the JVD structured index don't yet overlap on hardware family. Surface
    # it loudly instead of letting it hide in a JSON blob.
    compat = (bundle.get("jvd_reference") or {}).get("compatibility_check")
    if compat and not compat["any_bom_sku_matches_jvd_platform"]:
        print(
            "\nNOTE: this run demonstrates a real current gap -- the JVD "
            f"design {design_id!r} does not list any platform matching "
            f"{exact_sku!r}'s family. Treat the JVD reference as directional "
            "guidance only until the catalog and JVD index cover the same "
            "hardware families (tracked in the project plan)."
        )
    return 0 if review["ready_for_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
