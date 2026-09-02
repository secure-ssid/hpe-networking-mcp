"""Compose a reviewable design bundle from resolved SKUs, an optional JVD
design reference, and an operator-authored topology sketch.

This module is the canonical requirements -> SKU -> JVD -> topology -> BOM
artifact model described in the project plan. It deliberately does **not**
resolve ambiguity itself: callers must already have an exact SKU (from
:mod:`hpe_networking_mcp.pipeline.clients.hardware_catalog`) and, if used, an
exact JVD design id (from
:mod:`hpe_networking_mcp.pipeline.clients.jvd_catalog`). This module's only
job is deterministic composition and explicit field labeling -- it never
guesses a SKU, never invents a JVD reference, and never renders or pushes
device configuration.

Every field in the resulting bundle is labeled with exactly one of:

- ``official``: sourced verbatim from a catalog/JVD provenance record.
- ``operator_input``: supplied by the caller (role, quantity, topology
  sketch) and not independently verified.
- ``derived``: computed by this module from official + operator_input data
  (for example, per-role subtotals).
- ``unknown``: could not be resolved; always paired with a warning
  explaining why, never silently dropped.

The bundle is a *review* artifact only -- see the JVD design/build boundary
in ``docs/juniper-mist-jvd.md``. Diagram export (Draw.io/Graphviz/NeXt UI)
is intentionally left to the existing ``design-core`` exporters
(``mcp_servers/design_lib``); this module only produces the canonical
``topology`` dict those exporters already accept via ``parse_model``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpe_networking_mcp.mcp_servers.design_lib.model import parse_model
from hpe_networking_mcp.pipeline.clients import hardware_catalog, jvd_catalog

MAX_LINE_ITEMS = 100


def _resolve_line_item(
    item: dict[str, Any], *, catalog_db_path: Path
) -> dict[str, Any]:
    role = str(item.get("role") or "unspecified").strip()[:80]
    sku_query = item.get("sku")
    quantity_raw = item.get("quantity", 1)
    try:
        quantity = int(quantity_raw)
        if quantity < 1:
            raise ValueError
    except (TypeError, ValueError):
        return {
            "ok": False,
            "role": role,
            "requested_sku": sku_query,
            "warning": f"quantity {quantity_raw!r} must be a positive integer",
            "field_labels": {"role": "operator_input", "quantity": "unknown"},
        }
    if not isinstance(sku_query, str) or not sku_query.strip():
        return {
            "ok": False,
            "role": role,
            "requested_sku": sku_query,
            "quantity": quantity,
            "warning": "sku must be a non-empty string",
            "field_labels": {
                "role": "operator_input",
                "quantity": "operator_input",
                "sku": "unknown",
            },
        }
    lookup = hardware_catalog.search(sku_query, db_path=catalog_db_path)
    if lookup.get("match_type") != "exact_sku":
        return {
            "ok": False,
            "role": role,
            "requested_sku": sku_query,
            "quantity": quantity,
            "warning": (
                f"{sku_query!r} did not resolve to an exact catalog SKU "
                f"(match_type={lookup.get('match_type', 'error')!r}); resolve the exact "
                "SKU with search_hardware_catalog before adding it to a design bundle"
            ),
            "match_type": lookup.get("match_type"),
            "candidates": lookup.get("results", []),
            "field_labels": {
                "role": "operator_input",
                "quantity": "operator_input",
                "sku": "unknown",
            },
        }
    product = lookup["results"][0]
    return {
        "ok": True,
        "role": role,
        "quantity": quantity,
        "sku": product["sku"],
        "vendor": product["vendor"],
        "model": product["model"],
        "family": product["family"],
        "device_type": product["device_type"],
        "summary": product["summary"],
        "source": product["source"],
        "lifecycle": product["lifecycle"],
        "field_labels": {
            "role": "operator_input",
            "quantity": "operator_input",
            "sku": "official",
            "vendor": "official",
            "model": "official",
            "family": "official",
            "device_type": "official",
            "summary": "official",
            "source": "official",
            "lifecycle": "official",
        },
    }


def _resolve_jvd_reference(design_id: str, *, jvd_db_path: Path) -> dict[str, Any]:
    lookup = jvd_catalog.get_design(design_id, db_path=jvd_db_path)
    if not lookup.get("ok"):
        return {
            "ok": False,
            "requested_id": design_id,
            "warning": lookup.get(
                "error", f"JVD design {design_id!r} could not be resolved"
            ),
            "guidance": lookup.get("guidance"),
        }
    design = lookup["result"]
    return {
        "ok": True,
        "id": design["id"],
        "name": design["name"],
        "area": design["area"],
        "description": design["description"],
        "platforms": design["platforms"],
        "os": design["os"],
        "source": design["source"],
        "provenance": lookup["provenance"],
        "field_labels": {
            "id": "official",
            "name": "official",
            "area": "official",
            "description": "official",
            "platforms": "official",
            "os": "official",
            "source": "official",
            "provenance": "official",
        },
    }


def _compatibility_check(
    resolved_items: list[dict[str, Any]], jvd_reference: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort advisory overlap check between BOM SKUs and JVD platforms.

    This is intentionally advisory, not authoritative: it never blocks a
    bundle or removes a line item. It flags when *none* of the resolved BOM
    SKUs share their family/model with the referenced JVD's official
    ``platforms`` list, which usually means the JVD was written for a
    different hardware family (for example, a Data Center QFX/PTX/ACX
    fabric design referenced against Campus/Branch EX access switches) and
    the operator should treat the JVD as directional guidance only, not a
    literal bill of materials.
    """
    platforms = {str(p).strip().casefold() for p in jvd_reference.get("platforms", [])}
    matched_skus: list[str] = []
    for item in resolved_items:
        family = str(item.get("family", "")).strip().casefold()
        model = str(item.get("model", "")).strip().casefold()
        if any(family and family in platform for platform in platforms) or any(
            model and (model in platform or platform in model) for platform in platforms
        ):
            matched_skus.append(item["sku"])
    return {
        "any_bom_sku_matches_jvd_platform": bool(matched_skus),
        "matched_skus": matched_skus,
        "note": (
            "advisory only: no BOM SKU's family/model overlaps this JVD's official "
            "platforms list, so treat the JVD as directional guidance rather than a "
            "literal bill of materials"
            if not matched_skus
            else "at least one BOM SKU's family/model overlaps this JVD's official platforms"
        ),
        "field_labels": {
            "any_bom_sku_matches_jvd_platform": "derived",
            "matched_skus": "derived",
            "note": "derived",
        },
    }


def build_design_bundle(
    *,
    title: str,
    line_items: list[dict[str, Any]],
    topology: dict[str, Any] | None = None,
    jvd_design_id: str | None = None,
    catalog_db_path: Path = hardware_catalog.DB_PATH,
    jvd_db_path: Path = jvd_catalog.DB_PATH,
) -> dict[str, Any]:
    """Compose a reviewable BOM + topology + JVD-reference design bundle.

    ``line_items`` must each carry a ``sku`` that resolves to an exact
    catalog SKU (ambiguity must already be resolved by the caller via
    ``search_hardware_catalog``); unresolved items are reported under
    ``unresolved_line_items`` with an explicit warning rather than being
    silently dropped or guessed. ``topology`` (if given) must match the
    ``design_lib.model`` schema and is validated the same way the
    ``design-core`` diagram exporters validate it. ``jvd_design_id`` (if
    given) must be an exact id from ``search_jvd_designs``/``get_design``.
    """
    clean_title = str(title or "Untitled design").strip()[:120]
    if not isinstance(line_items, list) or not line_items:
        return {"ok": False, "error": "line_items must be a non-empty list"}
    if len(line_items) > MAX_LINE_ITEMS:
        return {"ok": False, "error": f"line_items exceeds max {MAX_LINE_ITEMS}"}

    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for raw_item in line_items:
        if not isinstance(raw_item, dict):
            unresolved.append({"ok": False, "warning": "line item must be an object"})
            continue
        item = _resolve_line_item(raw_item, catalog_db_path=catalog_db_path)
        (resolved if item["ok"] else unresolved).append(item)

    bom_total_units = sum(item["quantity"] for item in resolved)

    topology_result: dict[str, Any] | None = None
    topology_error: str | None = None
    if topology is not None:
        try:
            model = parse_model(topology)
            topology_result = model.to_dict()
        except ValueError as exc:
            topology_error = str(exc)

    jvd_reference: dict[str, Any] | None = None
    if jvd_design_id:
        jvd_reference = _resolve_jvd_reference(jvd_design_id, jvd_db_path=jvd_db_path)
        if jvd_reference["ok"] and resolved:
            jvd_reference["compatibility_check"] = _compatibility_check(resolved, jvd_reference)

    warnings: list[str] = [str(item["warning"]) for item in unresolved if item.get("warning")]
    if topology_error:
        warnings.append(f"topology: {topology_error}")
    if jvd_reference is not None and not jvd_reference["ok"]:
        warnings.append(f"jvd_design_id: {jvd_reference['warning']}")
    elif (
        jvd_reference is not None
        and "compatibility_check" in jvd_reference
        and not jvd_reference["compatibility_check"]["any_bom_sku_matches_jvd_platform"]
    ):
        warnings.append(
            f"jvd_design_id: no BOM SKU's family/model overlaps {jvd_reference['id']!r}'s "
            "official platforms list; treat this JVD as directional guidance only"
        )

    bundle: dict[str, Any] = {
        "ok": not unresolved and topology_error is None,
        "title": clean_title,
        "bom": {
            "line_items": resolved,
            "total_units": bom_total_units,
            "field_labels": {"line_items": "official+operator_input", "total_units": "derived"},
        },
        "unresolved_line_items": unresolved,
        "warnings": warnings,
        "boundary": (
            "review-only artifact: no live device configuration is rendered or pushed; "
            "see docs/juniper-mist-jvd.md for the JVD design/build boundary"
        ),
    }
    if topology_result is not None:
        bundle["topology"] = topology_result
    elif topology_error:
        bundle["topology_error"] = topology_error
    if jvd_reference is not None:
        bundle["jvd_reference"] = jvd_reference
    return bundle
