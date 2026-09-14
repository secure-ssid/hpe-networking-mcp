"""Fail-closed validation gates for review artifacts (design bundles).

This module answers exactly one question: *is this generated artifact safe
to hand to a human reviewer as "ready"*? It never renders or pushes
configuration itself -- see the JVD design/build boundary in
``docs/juniper-mist-jvd.md``. It also never repeats the underlying
composition logic in :mod:`hpe_networking_mcp.pipeline.design_bundle`; it
only inspects an already-built bundle and classifies its problems as
**blocking** (the artifact must not be presented as ready-for-review) or
**advisory** (worth a reviewer's attention but not a hard stop).

The core rule is fail-closed: an artifact is ``ready_for_review`` only when
there are zero blocking reasons. Any unresolved SKU, invalid topology, or
missing safety boundary statement blocks the artifact. A JVD/BOM hardware
family mismatch (from ``design_bundle``'s advisory-only
``compatibility_check``) is surfaced as an advisory note, never a blocker,
because it is inherently a judgment call for the human reviewer.
"""

from __future__ import annotations

from typing import Any

REQUIRED_BOUNDARY_PHRASE = "no live device configuration is rendered or pushed"


def validate_for_review(bundle: dict[str, Any]) -> dict[str, Any]:
    """Classify a design bundle's problems as blocking or advisory.

    Returns a dict with ``ready_for_review`` (bool), ``blocking_reasons``
    (list[str], empty means the artifact may be presented as ready),
    ``advisory_notes`` (list[str], never blocks review), and ``checked``
    (list[str] naming every gate that was evaluated, so a caller/reviewer
    can see this was not a partial check).
    """
    if not isinstance(bundle, dict):
        return {
            "ready_for_review": False,
            "blocking_reasons": ["bundle must be an object"],
            "advisory_notes": [],
            "checked": [],
        }

    blocking: list[str] = []
    advisory: list[str] = []
    checked: list[str] = []

    checked.append("unresolved_line_items")
    unresolved = bundle.get("unresolved_line_items") or []
    if unresolved:
        for item in unresolved:
            requested = item.get("requested_sku") if isinstance(item, dict) else None
            blocking.append(
                f"unresolved BOM line item (requested_sku={requested!r}) — resolve the "
                "exact SKU with search_hardware_catalog before this artifact is reviewable"
            )

    checked.append("topology_error")
    topology_error = bundle.get("topology_error")
    if topology_error:
        blocking.append(f"invalid topology: {topology_error}")

    checked.append("bom_present")
    bom = bundle.get("bom") if isinstance(bundle.get("bom"), dict) else {}
    line_items = bom.get("line_items") or []
    if not line_items:
        blocking.append("bom has no resolved line items — a design bundle needs at least one")

    checked.append("boundary_statement")
    boundary = bundle.get("boundary")
    if not isinstance(boundary, str) or REQUIRED_BOUNDARY_PHRASE not in boundary:
        blocking.append(
            "missing or altered review-only boundary statement — every artifact must "
            f"explicitly state '{REQUIRED_BOUNDARY_PHRASE}'"
        )

    checked.append("jvd_reference")
    jvd_reference = bundle.get("jvd_reference")
    if isinstance(jvd_reference, dict):
        if not jvd_reference.get("ok", True):
            blocking.append(
                f"jvd_reference did not resolve: {jvd_reference.get('warning', 'unknown error')}"
            )
        else:
            compat = jvd_reference.get("compatibility_check")
            if isinstance(compat, dict) and not compat.get("any_bom_sku_matches_jvd_platform"):
                advisory.append(
                    f"JVD design {jvd_reference.get('id')!r} does not share a hardware family "
                    "with any resolved BOM SKU — treat it as directional guidance only, "
                    "confirm applicability manually before relying on it"
                )
            coverage_note = (jvd_reference.get("provenance") or {}).get("coverage_note")
            if coverage_note:
                advisory.append(f"JVD coverage note: {coverage_note}")

    checked.append("field_labels_present")
    for item in line_items:
        if isinstance(item, dict) and not item.get("field_labels"):
            blocking.append(
                f"BOM line item sku={item.get('sku')!r} is missing field_labels — every "
                "generated field must be labeled official/operator_input/derived/unknown"
            )

    return {
        "ready_for_review": not blocking,
        "blocking_reasons": blocking,
        "advisory_notes": advisory,
        "checked": checked,
    }
