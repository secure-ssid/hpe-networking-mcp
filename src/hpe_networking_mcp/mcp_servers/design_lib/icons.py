"""Icon registry for diagram exporters.

In-repo assets under ``resources/diagram_icons/generic/`` are geometric SVGs
only. Vendor product / brand packs are **not** shipped (copyright).

Resolution order for Graphviz image nodes:
1. ``HPE_MCP_DIAGRAM_ICON_DIR`` (if set)
2. ``resources/diagram_icons/private/vendors/<vendor>/`` (gitignored local packs)
3. ``resources/diagram_icons/vendors/<vendor>/``
4. ``resources/diagram_icons/generic/``

See ``resources/diagram_icons/README.md`` for install paths and external sources.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

REPO_ROOT = repo_root()
DEFAULT_ICON_ROOT = REPO_ROOT / "resources" / "diagram_icons"
PRIVATE_ICON_ROOT = DEFAULT_ICON_ROOT / "private"

# Draw.io style overrides (generic geometric — no vendor trademarks)
_DRAWIO_ROLE_STYLE: dict[str, str] = {
    "cloud": "shape=mxgraph.networks.cloud;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "campus_ap": "ellipse;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "mist_ap": "ellipse;fillColor=#E1D5E7;strokeColor=#9673A6;",
    "clearpass": "shape=cylinder3;size=12;fillColor=#F5F5F5;strokeColor=#666666;",
    "core_switch": "shape=mxgraph.cisco.switches.workgroup_switch;fillColor=#D5E8D4;strokeColor=#82B366;",
}


def _icon_roots() -> list[Path]:
    """Roots searched for raster/SVG packs and Visio stencils."""
    roots: list[Path] = []
    env = os.getenv("HPE_MCP_DIAGRAM_ICON_DIR", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    # Prefer gitignored private packs over in-repo generics.
    if PRIVATE_ICON_ROOT.is_dir():
        roots.append(PRIVATE_ICON_ROOT)
    roots.append(DEFAULT_ICON_ROOT)
    return roots


def list_icons() -> dict[str, Any]:
    """List discoverable icon files, Visio stencils, and external sources."""
    found: list[dict[str, str]] = []
    stencils: list[dict[str, str]] = []
    for root in _icon_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            # Skip PPTX named dump listing blow-up; role/vendor paths are enough.
            try:
                rel = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
            except ValueError:
                rel = path.name
            if "hpe-technical-icons-2026/named/" in rel.replace("\\", "/"):
                continue
            suf = path.suffix.lower()
            if suf in {".svg", ".png", ".jpg", ".jpeg", ".gif"}:
                found.append(
                    {
                        "path": str(path),
                        "relative": rel,
                        "root": str(root),
                        "vendor_guess": _guess_vendor(rel),
                    }
                )
            elif suf in {".vss", ".vssx", ".vsx", ".vstx", ".vstm"}:
                stencils.append(
                    {
                        "path": str(path),
                        "relative": rel,
                        "root": str(root),
                        "format": suf.lstrip("."),
                        "pack": path.stem,
                        "vendor_guess": _guess_vendor(rel) or "hpe",
                    }
                )
            if len(found) >= 200 and len(stencils) >= 50:
                break

    tech_catalog = PRIVATE_ICON_ROOT / "hpe-technical-icons-2026" / "catalog.json"
    tech_meta: dict[str, Any] = {}
    if tech_catalog.is_file():
        try:
            raw = json.loads(tech_catalog.read_text(encoding="utf-8"))
            tech_meta = {
                "path": str(tech_catalog.parent),
                "named_count": raw.get("named_count"),
                "role_map": raw.get("role_map") or {},
                "source": raw.get("source"),
            }
        except (OSError, json.JSONDecodeError):
            tech_meta = {"path": str(tech_catalog.parent), "error": "catalog unreadable"}

    private = PRIVATE_ICON_ROOT
    return {
        "icon_count": len(found),
        "icons": found,
        "visio_stencil_count": len(stencils),
        "visio_stencils": stencils,
        "search_roots": [str(r) for r in _icon_roots()],
        "env_override": "HPE_MCP_DIAGRAM_ICON_DIR",
        "private_pack_dir": str(private),
        "hpe_technical_icons": tech_meta or None,
        "external_sources": [
            {
                "vendor": "hpe_technical_icons_pptx",
                "title": "HPE Technical Networking Icons 2026 (PPTX → local SVG roles)",
                "path": str(private / "hpe-technical-icons-2026"),
                "note": (
                    "Official HPE PPTX vectors extracted locally for Graphviz role icons. "
                    "Do not commit or redistribute. Install via "
                    "scripts/install_diagram_icon_packs.py"
                ),
            },
            {
                "vendor": "hpe_aruba_visio",
                "title": "HPE Aruba VisioCafe stencils (.vss) — local private install only",
                "path": str(private / "hpe-aruba-visio"),
                "note": (
                    "Licensed for local diagram creation. Do not commit, redistribute, "
                    "or reverse-engineer. Use in Microsoft Visio (My Shapes/HPE)."
                ),
            },
            {
                "vendor": "hpe_aruba_symbols",
                "title": "HPE Aruba Symbols Visio stencils (.vss)",
                "path": str(private / "hpe-aruba-symbols"),
                "note": "Local use only; not redistributed by hpe-networking-mcp.",
            },
            {
                "vendor": "juniper_official",
                "title": "Juniper public Visio icon packs + product photos (local private install)",
                "path": str(private / "juniper-official"),
                "url": "https://www.juniper.net/us/en/products/icons-and-stencils.html",
                "note": (
                    "Downloaded by scripts/install_diagram_icon_packs.py --juniper. "
                    "Visio .vss/.vssx for Visio; product PNGs mapped to vendors/juniper "
                    "and vendors/mist for Graphviz. Gitignored — do not redistribute."
                ),
            },
            {
                "vendor": "juniper_mist_image_library",
                "title": "Juniper image library (logos & product photos)",
                "url": (
                    "https://www.juniper.net/us/en/company/images/"
                    "image-library-logos-and-product-photos.html"
                ),
                "note": "Public catalog used for local product-photo role aliases.",
            },
            {
                "vendor": "flaticon_network_diagram",
                "title": "Flaticon free network-diagram icons (manual download + attribution)",
                "url": "https://www.flaticon.com/free-icons/network-diagram",
                "note": (
                    "Optional generic pack. Download SVGs manually under Flaticon license, "
                    "keep attribution, and place as "
                    "vendors/generic/<role>.svg or HPE_MCP_DIAGRAM_ICON_DIR/... "
                    "hpe-networking-mcp does not scrape or vendor Flaticon assets."
                ),
            },
            {
                "vendor": "generic",
                "title": "In-repo generic geometric SVGs",
                "path": str(DEFAULT_ICON_ROOT / "generic"),
            },
        ],
        "drawio_note": (
            "Draw.io export uses built-in mxgraph/Cisco shapes by default; "
            "PNG/SVG packs apply to Graphviz image nodes. HPE .vss Visio stencils "
            "are for Visio (and manual draw.io Visio import), not auto-embedded."
        ),
        "visio_role_hints": {
            "core_switch": "HPE-Aruba-Switches-*.vss",
            "agg_switch": "HPE-Aruba-Switches-*.vss",
            "access_switch": "HPE-Aruba-Switches-*.vss",
            "campus_ap": "HPE-Aruba-Wireless.vss / HPE-Aruba_Symbols-Access_Points.vss",
            "gateway": "HPE-Aruba-Gateways+Controllers.vss",
            "controller": "HPE-Aruba-Gateways+Controllers.vss",
            "clearpass": "HPE-Aruba-Security.vss",
            "firewall": "HPE-Aruba-Security.vss",
            "edgeconnect": "HPE-Aruba-EdgeConnect.vss",
            "server": "HPE-Compute-AI.vss / HPE-Synergy.vss",
        },
    }


def _guess_vendor(rel: str) -> str:
    parts = Path(rel).parts
    if "vendors" in parts:
        i = parts.index("vendors")
        if i + 1 < len(parts):
            return parts[i + 1].lower()
    if parts and parts[0] == "generic":
        return "generic"
    if parts and parts[0].startswith("hpe-"):
        return "hpe"
    return "unknown"


# Product photos (often 2k–4k px) blow up Graphviz layouts. Prefer compact
# diagram glyphs only. Override with HPE_MCP_DIAGRAM_ALLOW_LARGE_ICONS=1.
_MAX_DIAGRAM_ICON_BYTES = 120_000  # ~120 KB
_PRODUCT_PHOTO_NAME_HINTS = (
    "ap47",
    "ap45",
    "ap34",
    "ap24",
    "ap12",
    "ap63",
    "front",
    "photo",
    "-front",
)


def _allow_large_icons() -> bool:
    return os.getenv("HPE_MCP_DIAGRAM_ALLOW_LARGE_ICONS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_diagram_sized_icon(path: Path) -> bool:
    """Reject product-photo sized assets for topology node images."""
    if _allow_large_icons():
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return False
    name = path.name.lower()
    # Keep photos/ trees for inventory, never auto-embed in topology nodes.
    parts = {p.lower() for p in path.parts}
    if "photos" in parts or "product-photos" in parts or "image-library" in parts:
        return False
    if any(h in name for h in _PRODUCT_PHOTO_NAME_HINTS) and size > 40_000:
        return False
    # Small SVG/PNG role glyphs are fine; multi-MB product shots are not.
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"} and size > _MAX_DIAGRAM_ICON_BYTES:
        return False
    return True


def _pick_first_icon(rels: list[str]) -> Path | None:
    """Pick best icon preserving candidate order (role before default).

    Within the first matching specificity tier, prefer compact PNG/JPG for
    Graphviz, then small SVG. Never prefer a generic ``default.*`` over a
    role-specific file just because the default is smaller on disk.
    """
    scored: list[tuple[int, int, int, Path]] = []
    for root in _icon_roots():
        for idx, rel in enumerate(rels):
            path = root / rel
            if not path.is_file() or not _is_diagram_sized_icon(path):
                continue
            suf = path.suffix.lower()
            try:
                nbytes = path.stat().st_size
            except OSError:
                continue
            # format preference within the same candidate index
            if suf in {".png", ".jpg", ".jpeg"} and nbytes <= _MAX_DIAGRAM_ICON_BYTES:
                fmt = 0
            elif suf == ".svg" and nbytes <= 80_000:
                fmt = 1
            else:
                fmt = 2
            scored.append((idx, fmt, nbytes, path))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return scored[0][3]


def icon_path_for(vendor: str, role: str) -> Path | None:
    """Resolve best *diagram-sized* icon for vendor+role.

    Skips oversized product photos (common under Mist/Juniper packs) so Graphviz
    nodes stay small and readable. Falls back to generic geometric icons.
    """
    vendor = (vendor or "generic").lower()
    role = (role or "generic").lower()
    # private root already includes vendors/; default root has vendors/ + generic/
    vendor_candidates = [
        f"vendors/{vendor}/{role}.png",
        f"vendors/{vendor}/{role}.svg",
        f"vendors/{vendor}/{role}.jpg",
        f"vendors/{vendor}/{role}.jpeg",
        f"vendors/{vendor}/default.png",
        f"vendors/{vendor}/default.svg",
        f"vendors/{vendor}/default.jpg",
        # allow role files dropped at pack root
        f"{role}.png",
        f"{role}.svg",
        f"by_role/{role}.png",
        f"by_role/{role}.svg",
    ]
    generic_candidates = [
        f"generic/{role}.png",
        f"generic/{role}.svg",
        f"generic/default.svg",
        f"vendors/generic/{role}.png",
        f"vendors/generic/{role}.svg",
        f"vendors/generic/default.svg",
    ]
    return _pick_first_icon(vendor_candidates) or _pick_first_icon(generic_candidates)


def resolve_icon(vendor: str, role: str) -> dict[str, Any]:
    path = icon_path_for(vendor, role)
    skipped_large = False
    if path is None:
        # Surface why a vendor PNG might have been ignored (oversized product photo).
        for root in _icon_roots():
            candidate = root / f"vendors/{vendor}/{role}.png"
            if candidate.is_file() and not _is_diagram_sized_icon(candidate):
                skipped_large = True
                break
    return {
        "vendor": vendor,
        "role": role,
        "path": str(path) if path else None,
        "found": path is not None,
        "skipped_oversized_product_photo": skipped_large,
        "drawio_style": style_for_node(vendor, role).get("drawio_style"),
    }


def style_for_node(vendor: str, role: str) -> dict[str, Any]:
    """Return optional Draw.io style override for a node."""
    role = (role or "generic").lower()
    style = _DRAWIO_ROLE_STYLE.get(role)
    return {"drawio_style": style, "vendor": vendor, "role": role}
