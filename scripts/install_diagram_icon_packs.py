#!/usr/bin/env python3
"""Install local (gitignored) diagram icon packs from Downloads or explicit paths.

Supports:
  - HPE VisioCafe / Recent ``.vss`` zip packs
  - HPE Aruba Symbols ``.vss`` zip
  - HPE Technical Networking Icons PPTX (extracts SVGs + role aliases)
  - Juniper public Visio icon ZIPs + Mist/product photos (local only)

Nothing from these packs is committed. Output always lands under
``resources/diagram_icons/private/`` (gitignored).

Examples:
  uv run python scripts/install_diagram_icon_packs.py --from-downloads
  uv run python scripts/install_diagram_icon_packs.py \\
      --pptx ~/Downloads/HPE_Technical_Networking_Icons_2026.pptx
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
PRIVATE = REPO / "resources" / "diagram_icons" / "private"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

SKIP_EXACT = {
    "confidential | authorized",
    "technical icons",
    "technology specific icons",
    "generic icons",
    "generic",
    "technology",
    "specific icons",
    "native powerpoint vectors",
    "february 2026",
    "transparency",
    "scalability",
    "minimum file size",
    "confidential",
}
SKIP_PREFIX = (
    "why use",
    "the vector",
    "to use",
    "the icons",
    "these icons",
    "aggregation block",
    "connection between",
    "core, service",
    "core/rr",
    "no impact",
    "centralized gw",
    "containerized",
)

ROLE_MATCHERS: list[tuple[str, list[str]]] = [
    (
        "campus_ap",
        [
            "access_point",
            "access_point_alt",
            "access_point_desktop",
            "access_point_smb",
            "access_outdoor",
            "access_mounted",
            "ap_635",
        ],
    ),
    ("mist_ap", ["mist"]),
    (
        "core_switch",
        [
            "layer_3_switch",
            "distributed_services_switch",
            "distributed_service_switch_10k",
            "aos_cx_software",
        ],
    ),
    ("agg_switch", ["layer_3_switch", "layer_2_switch"]),
    ("access_switch", ["layer_2_switch", "layer_2_switch_micro"]),
    (
        "gateway",
        [
            "gateway",
            "gateway_branch",
            "gateway_headend",
            "gateway_mobility",
            "gateway_micro",
            "2_gateway_cluster",
        ],
    ),
    ("controller", ["gateway_mobility", "controller", "mobility"]),
    (
        "firewall",
        ["firewall", "gateway_firewall", "firewall_active", "east_west_firewall"],
    ),
    (
        "clearpass",
        ["certificate", "policy_service_manager", "access_authorized", "clearpass"],
    ),
    ("router", ["router", "bridge_wi_fi", "multiprotocol_label_switching"]),
    ("server", ["blade_server", "media_server", "proxy_server"]),
    (
        "cloud",
        [
            "cloud",
            "hpe_aruba_networking_central_cloud",
            "hpe_aruba_networking_central_on_premise",
            "cloud_guest",
        ],
    ),
    ("client", ["client_insights", "applications"]),
    (
        "edgeconnect",
        [
            "edge_connect_physical",
            "edge_connect_virtual",
            "edge_connect_cloud",
            "aruba_orchestrator",
            "edgeconnect",
        ],
    ),
]


def slugify(name: str) -> str:
    s = name.strip().lower().replace("–", "-").replace("—", "-").replace("/", "-")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")[:80] or "icon"


def clean_labels(texts: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in texts:
        low = t.lower().strip()
        if not low or low.isdigit() or low in SKIP_EXACT:
            continue
        if any(low.startswith(p) for p in SKIP_PREFIX) or len(t) > 80:
            continue
        if low not in seen:
            seen.add(low)
            out.append(t.strip())
    return out


def _copy_vss_from_zip(zip_path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if name.lower().endswith((".vss", ".vssx")) and not info.is_dir():
                target = dest / name
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    return count


def install_visio_zips(paths: list[Path], dest_name: str) -> dict:
    dest = PRIVATE / dest_name
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    for zp in paths:
        if not zp.is_file():
            continue
        if zp.suffix.lower() == ".zip":
            total += _copy_vss_from_zip(zp, dest)
        elif zp.suffix.lower() in {".vss", ".vssx"}:
            shutil.copy2(zp, dest / zp.name)
            total += 1
    (dest / "NOTICE.txt").write_text(
        "HPE / Aruba Visio stencils — LOCAL diagram creation only.\n"
        "Do not commit, redistribute, upload, or reverse-engineer.\n",
        encoding="utf-8",
    )
    return {"dest": str(dest), "vss_count": total}


def install_pptx(pptx: Path) -> dict:
    if not pptx.is_file():
        raise FileNotFoundError(pptx)

    out = PRIVATE / "hpe-technical-icons-2026"
    if out.exists():
        shutil.rmtree(out)
    named = out / "named"
    by_role = out / "by_role"
    named.mkdir(parents=True)
    by_role.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="hpe-pptx-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(pptx) as zf:
            zf.extractall(root)
        media = root / "ppt" / "media"
        slides = sorted(
            (root / "ppt" / "slides").glob("slide*.xml"),
            key=lambda p: int(re.search(r"slide(\d+)", p.name).group(1)),
        )
        label_to_file: dict[str, Path] = {}
        label_meta: list[dict] = []
        for slide in slides:
            n = int(re.search(r"slide(\d+)", slide.name).group(1))
            tree = ET.parse(slide)
            texts = [
                t.text.strip()
                for t in tree.iter(A_NS + "t")
                if t.text and t.text.strip()
            ]
            labels = clean_labels(texts)
            rels = root / "ppt" / "slides" / "_rels" / f"slide{n}.xml.rels"
            imgs: list[str] = []
            if rels.exists():
                for rel in ET.parse(rels).getroot():
                    target = rel.attrib.get("Target", "")
                    if "media/" in target:
                        imgs.append(Path(target).name)
            svgs = [
                media / im
                for im in imgs
                if (media / im).is_file() and im.lower().endswith(".svg")
            ]
            if not labels or not svgs:
                continue
            k = len(labels)
            chunk = max(1, len(svgs) // k)
            for i, lab in enumerate(labels):
                start = i * chunk
                end = (i + 1) * chunk if i < k - 1 else len(svgs)
                group = svgs[start:end]
                if not group:
                    continue
                best = max(group, key=lambda p: p.stat().st_size)
                if best.stat().st_size < 400:
                    continue
                slug = slugify(lab)
                if (
                    slug in label_to_file
                    and label_to_file[slug].stat().st_size >= best.stat().st_size
                ):
                    continue
                label_to_file[slug] = best
                label_meta.append(
                    {
                        "label": lab,
                        "slug": slug,
                        "slide": n,
                        "source_media": best.name,
                        "bytes": best.stat().st_size,
                        "sha1": hashlib.sha1(best.read_bytes()).hexdigest(),
                    }
                )

        for meta in label_meta:
            src = label_to_file[meta["slug"]]
            shutil.copy2(src, named / f"{meta['slug']}.svg")

    slug_set = set(label_to_file)
    role_resolved: dict[str, str] = {}
    for role, prefs in ROLE_MATCHERS:
        chosen = None
        for pref in prefs:
            if pref in slug_set:
                chosen = pref
                break
            hits = sorted(
                (s for s in slug_set if pref in s), key=lambda s: (len(s), s)
            )
            if hits:
                chosen = hits[0]
                break
        if chosen:
            role_resolved[role] = chosen

    vendors_root = PRIVATE / "vendors"
    for vendor in ("aruba", "hpe"):
        (vendors_root / vendor).mkdir(parents=True, exist_ok=True)

    for role, slug in role_resolved.items():
        src = named / f"{slug}.svg"
        shutil.copy2(src, by_role / f"{role}.svg")
        shutil.copy2(src, vendors_root / "aruba" / f"{role}.svg")
        shutil.copy2(src, vendors_root / "hpe" / f"{role}.svg")

    if "campus_ap" in role_resolved:
        src = named / f"{role_resolved['campus_ap']}.svg"
        shutil.copy2(src, vendors_root / "aruba" / "default.svg")
        shutil.copy2(src, vendors_root / "hpe" / "default.svg")

    if "edgeconnect" in role_resolved:
        ec = vendors_root / "edgeconnect"
        ec.mkdir(parents=True, exist_ok=True)
        src = named / f"{role_resolved['edgeconnect']}.svg"
        shutil.copy2(src, ec / "default.svg")
        shutil.copy2(src, ec / "gateway.svg")
        shutil.copy2(src, ec / "edgeconnect.svg")

    if "clearpass" in role_resolved:
        cp = vendors_root / "clearpass"
        cp.mkdir(parents=True, exist_ok=True)
        src = named / f"{role_resolved['clearpass']}.svg"
        shutil.copy2(src, cp / "default.svg")
        shutil.copy2(src, cp / "clearpass.svg")

    if "mist_ap" in role_resolved:
        mist = vendors_root / "mist"
        mist.mkdir(parents=True, exist_ok=True)
        src = named / f"{role_resolved['mist_ap']}.svg"
        shutil.copy2(src, mist / "default.svg")
        shutil.copy2(src, mist / "mist_ap.svg")
        shutil.copy2(src, mist / "campus_ap.svg")

    catalog = {
        "source": pptx.name,
        "note": (
            "HPE confidential technical icons extracted for LOCAL diagram use only. "
            "Do not commit or redistribute."
        ),
        "named_count": len(label_meta),
        "role_map": role_resolved,
        "icons": [
            {**m, "path": f"named/{m['slug']}.svg"}
            for m in sorted(label_meta, key=lambda x: x["slug"])
        ],
    }
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    (out / "NOTICE.txt").write_text(
        "HPE Technical Networking Icons (from official PPTX).\n"
        "LOCAL diagram creation only. Do NOT commit, redistribute, or host publicly.\n",
        encoding="utf-8",
    )
    return {
        "dest": str(out),
        "named_count": len(label_meta),
        "roles": role_resolved,
        "vendors_dir": str(vendors_root),
    }




JUNIPER_ICON_ZIPS = [
    # Full set from https://www.juniper.net/us/en/products/icons-and-stencils.html
    "juniper-acx-series-icons.zip",
    "juniper-aide-icons.zip",
    "juniper-branch-srx-series-icons.zip",
    "juniper-bti-series-icons.zip",
    "juniper-ctp-series-icons.zip",
    "juniper-cx-series-icons.zip",
    "juniper-ex-series-icons.zip",
    "juniper-firewall-ipsec-vpn-icons.zip",
    "juniper-generic-visio.zip",
    "juniper-ja-series-icons.zip",
    "juniper-jsa-series-icons.zip",
    "juniper-ln-series-icons.zip",
    "juniper-mx-series-icons.zip",
    "juniper-nfx-series-icons.zip",
    "juniper-ocx1100-icons.zip",
    "juniper-ptx-series-icons.zip",
    "juniper-qfx-series-icons.zip",
    "juniper-session-smart-routers-icons.zip",
    "juniper-srx-series-icons.zip",
    "juniper-tcx-series-icons.zip",
]

JUNIPER_BASE = (
    "https://www.juniper.net/content/dam/www/assets/images/us/en/company/image-library"
)
JUNIPER_SITE = "https://www.juniper.net"

# product slug -> (role vendors map key, relative photo path under product-photos)
JUNIPER_PRODUCT_PHOTOS = [
    # Mist APs
    ("ap45", "access-points/ap45-front.png"),
    ("ap47", "access-points/ap47.png"),
    ("ap34", "access-points/ap34-front.png"),
    ("ap24", "access-points/ap24-front.png"),
    ("ap12", "access-points/ap12-front.png"),
    ("ap63", "access-points/ap63-front.png"),
    # switches / routers / security
    ("ex4400", "switches/ex4400-front.png"),
    ("ex4100-48t-48p", "switches/ex4100-front.png"),
    ("ex4650-48y", "switches/ex4650-48y-front.png"),
    ("qfx5120-48y", "switches/qfx5120-48y-front.png"),
    ("qfx10002-36q", "switches/qfx10002-36q-front.png"),
    ("srx380", "security/srx380-front.png"),
    ("mx204", "routers/mx204-front.png"),
    ("ssr1200", "routers/ssr1200-front.png"),
]


def _http_get(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "hpe-networking-mcp-local-icon-install/1.0"})
    try:
        with urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed vendor CDN URLs
            dest.write_bytes(resp.read())
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001 - best-effort local installer
        print(f"warn: failed {url}: {exc}", file=sys.stderr)
        return False


def _best_product_image_url(slug: str) -> str | None:
    page = f"{JUNIPER_SITE}/us/en/company/images/image-library-logos-and-product-photos/products/{slug}.html"
    req = Request(page, headers={"User-Agent": "hpe-networking-mcp-local-icon-install/1.0"})
    try:
        with urlopen(req, timeout=60) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        print(f"warn: product page {slug}: {exc}", file=sys.stderr)
        return None
    paths = re.findall(
        r"/content/dam/www/assets/images/us/en/image-library/[^\"']+\.(?:png|jpg|jpeg)",
        html,
        flags=re.I,
    )
    if not paths:
        return None
    def score(p: str) -> tuple[int, int]:
        pl = p.lower()
        s = 0
        if "front-low.png" in pl:
            s += 100
        elif "front.png" in pl:
            s += 90
        elif "front-low" in pl:
            s += 80
        elif "front" in pl:
            s += 60
        if "packaging" in pl or "lbox" in pl:
            s -= 40
        return (-s, len(p))
    paths = sorted(set(paths), key=score)
    return JUNIPER_SITE + paths[0]



def _download_all_product_photos(photos_root: Path) -> list[str]:
    """Download best front image for every product in the public image library."""
    import concurrent.futures

    page = (
        f"{JUNIPER_SITE}/us/en/company/images/"
        "image-library-logos-and-product-photos/products.html"
    )
    req = Request(page, headers={"User-Agent": "hpe-networking-mcp-local-icon-install/1.0"})
    try:
        with urlopen(req, timeout=60) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        print(f"warn: products index: {exc}", file=sys.stderr)
        return []

    slug_re = re.compile(
        r"/us/en/company/images/image-library-logos-and-product-photos/"
        r"products/([A-Za-z0-9._-]+)\.html"
    )
    slugs = sorted(set(slug_re.findall(html)))

    def category(slug: str) -> str:
        s = slug.lower()
        if s.startswith("ap") or s.startswith("bt"):
            return "access-points"
        if s.startswith(("ex", "qfx", "ocx")):
            return "switches"
        if s.startswith("srx"):
            return "security"
        if s.startswith(("mx", "ptx", "acx", "ssr", "nfx", "ctp", "ln", "tcx", "ja")):
            return "routers"
        return "other"

    def one(slug: str) -> str | None:
        url = _best_product_image_url(slug)
        if not url:
            return None
        ext = Path(url).suffix.lower() or ".jpg"
        dest = photos_root / category(slug) / f"{slug}{ext}"
        if dest.is_file() and dest.stat().st_size > 2000:
            return str(dest.relative_to(photos_root))
        if _http_get(url, dest):
            return str(dest.relative_to(photos_root))
        return None

    ok: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for rel in ex.map(one, slugs):
            if rel:
                ok.append(rel)
    return ok


def install_juniper_public(*, skip_large_zips: bool = False, all_photos: bool = False) -> dict:
    """Download public Juniper Visio packs + product photos into private/.

    Assets remain gitignored. Follow Juniper brand/use terms.
    """
    root = PRIVATE / "juniper-official"
    zdir = root / "zips"
    visio = root / "visio"
    photos = root / "product-photos"
    logos = root / "logos"
    for d in (zdir, visio, photos, logos):
        d.mkdir(parents=True, exist_ok=True)

    zips_ok = []
    for name in JUNIPER_ICON_ZIPS:
        dest = zdir / name
        if dest.is_file() and dest.stat().st_size > 1000:
            zips_ok.append(name)
            continue
        if skip_large_zips and name in {
            "juniper-ex-series-icons.zip",
            "juniper-qfx-series-icons.zip",
            "juniper-mx-series-icons.zip",
            "juniper-ptx-series-icons.zip",
            "juniper-acx-series-icons.zip",
            "juniper-srx-series-icons.zip",
        }:
            continue
        url = f"{JUNIPER_BASE}/{name}"
        if _http_get(url, dest):
            zips_ok.append(name)

    # extract vss/vssx/pdf (skip __MACOSX)
    extracted = 0
    for zp in sorted(zdir.glob("*.zip")):
        try:
            with zipfile.ZipFile(zp) as zf:
                for info in zf.infolist():
                    base = Path(info.filename).name
                    if info.is_dir() or "__MACOSX" in info.filename or base.startswith("."):
                        continue
                    if not base.lower().endswith((".vss", ".vssx", ".pdf")):
                        continue
                    target = visio / f"{zp.stem}__{base}"
                    if target.is_file() and target.stat().st_size > 0:
                        extracted += 1
                        continue
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    extracted += 1
        except zipfile.BadZipFile:
            print(f"warn: bad zip {zp.name}", file=sys.stderr)

    # logo
    _http_get(
        "https://www.juniper.net/content/dam/www/assets/images/global/juniper_black-rgb-header.svg",
        logos / "juniper-logo-black.svg",
    )

    photo_ok = []
    for slug, rel in JUNIPER_PRODUCT_PHOTOS:
        dest = photos / rel
        if dest.is_file() and dest.stat().st_size > 1000:
            photo_ok.append(rel)
            continue
        url = _best_product_image_url(slug)
        if url and _http_get(url, dest):
            photo_ok.append(rel)


    if all_photos:
        extra = _download_all_product_photos(photos)
        for rel in extra:
            if rel not in photo_ok:
                photo_ok.append(rel)

    # Role aliases for Graphviz/topology nodes: use *compact* glyphs only.
    # Full product photos stay under product-photos/ (inventory / manual use).
    # Embedding multi-megapixel AP photos as node images makes layouts unusable.
    vendors = PRIVATE / "vendors"
    jdir = vendors / "juniper"
    mdir = vendors / "mist"
    adir = vendors / "aruba"
    repo_generic = Path(__file__).resolve().parents[1] / "resources" / "diagram_icons" / "generic"
    jdir.mkdir(parents=True, exist_ok=True)
    mdir.mkdir(parents=True, exist_ok=True)

    def _cp(src: Path, *dests: Path) -> None:
        if not src.is_file():
            return
        for d in dests:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)

    def _compact_role_source(role: str) -> Path | None:
        """Prefer small HPE role PNG/SVG, then in-repo generic SVG — never product photos."""
        for base in (adir, repo_generic):
            for name in (f"{role}.png", f"{role}.svg", "default.png", "default.svg"):
                cand = base / name
                if not cand.is_file():
                    continue
                try:
                    if cand.stat().st_size <= 120_000:
                        return cand
                except OSError:
                    continue
        return None

    # Clear prior role slots (including leftover product photos)
    for d in (mdir, jdir):
        for stale in d.glob("*"):
            if stale.is_file() and stale.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".svg",
                ".gif",
            }:
                stale.unlink(missing_ok=True)

    mist_roles = ("mist_ap", "campus_ap", "default")
    juniper_roles = (
        "access_switch",
        "agg_switch",
        "core_switch",
        "campus_ap",
        "mist_ap",
        "firewall",
        "router",
        "gateway",
        "default",
        "cloud",
    )
    for role in mist_roles:
        src = _compact_role_source("mist_ap" if role != "cloud" else "cloud") or _compact_role_source(
            role
        )
        if src:
            _cp(src, mdir / f"{role}{src.suffix.lower()}")
    for role in juniper_roles:
        src = _compact_role_source(role)
        if src:
            _cp(src, jdir / f"{role}{src.suffix.lower()}")
    # Optional logo for cloud if present and small enough
    logo = logos / "juniper-logo-black.svg"
    if logo.is_file() and logo.stat().st_size <= 80_000:
        _cp(logo, jdir / "cloud.svg")

    catalog = {
        "source": "juniper.net public icons-and-stencils + image library",
        "note": (
            "LOCAL only — gitignored. Do not redistribute. Follow Juniper brand terms. "
            "Product photos are under product-photos/ only; vendor role icons are compact "
            "diagram glyphs (not product photography)."
        ),
        "zip_count": len(zips_ok),
        "visio_file_count": extracted,
        "product_photo_count": len(photo_ok),
        "zips": zips_ok,
        "photos": photo_ok,
        "vendor_role_files": {
            "juniper": sorted(p.name for p in jdir.glob("*") if p.is_file()),
            "mist": sorted(p.name for p in mdir.glob("*") if p.is_file()),
        },
    }
    (root / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    (root / "NOTICE.txt").write_text(
        "Juniper / Mist public image-library assets and Visio icon packs.\n"
        "Downloaded from juniper.net for LOCAL diagram creation only.\n"
        "Do not commit or redistribute. Follow Juniper brand guidelines.\n"
        "https://www.juniper.net/us/en/products/icons-and-stencils.html\n"
        "https://www.juniper.net/us/en/company/images/image-library-logos-and-product-photos.html\n",
        encoding="utf-8",
    )
    return {"dest": str(root), **catalog}


def discover_downloads(downloads: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {
        "visio_networking": [],
        "visio_recent": [],
        "visio_symbols": [],
        "pptx": [],
    }
    if not downloads.is_dir():
        return found
    for p in sorted(downloads.iterdir()):
        name = p.name.lower()
        if not p.is_file():
            continue
        if "hpe-aruba-networking" in name and name.endswith(".zip"):
            found["visio_networking"].append(p)
        elif "hpe-recent" in name and name.endswith(".zip"):
            found["visio_recent"].append(p)
        elif "hpe-aruba-symbols" in name and name.endswith(".zip"):
            found["visio_symbols"].append(p)
        elif "technical_networking_icons" in name and name.endswith(".pptx"):
            found["pptx"].append(p)
        elif name.endswith(".pptx") and "hpe" in name and "icon" in name:
            found["pptx"].append(p)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-downloads",
        action="store_true",
        help="Auto-discover packs under ~/Downloads",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=Path.home() / "Downloads",
        help="Downloads directory (default: ~/Downloads)",
    )
    parser.add_argument("--pptx", type=Path, help="HPE Technical Networking Icons PPTX")
    parser.add_argument(
        "--visio-zip",
        action="append",
        default=[],
        type=Path,
        help="VisioCafe / Recent zip or .vss (repeatable)",
    )
    parser.add_argument(
        "--juniper",
        action="store_true",
        help="Download public Juniper Visio packs + Mist/product photos (local only)",
    )
    parser.add_argument(
        "--juniper-all-photos",
        action="store_true",
        help="Also download full Juniper product image library (~200 products, local only)",
    )
    parser.add_argument(
        "--symbols-zip",
        action="append",
        default=[],
        type=Path,
        help="HPE Aruba Symbols zip or .vss (repeatable)",
    )
    args = parser.parse_args(argv)

    PRIVATE.mkdir(parents=True, exist_ok=True)
    report: dict = {"private": str(PRIVATE), "installed": []}

    pptx = args.pptx
    visio = list(args.visio_zip)
    symbols = list(args.symbols_zip)

    if args.from_downloads:
        found = discover_downloads(args.downloads_dir)
        if not pptx and found["pptx"]:
            pptx = found["pptx"][-1]
        visio.extend(found["visio_networking"])
        visio.extend(found["visio_recent"])
        symbols.extend(found["visio_symbols"])

    if visio:
        # Prefer latest networking zip + any recent
        net = [p for p in visio if "networking" in p.name.lower()]
        recent = [p for p in visio if "recent" in p.name.lower()]
        other = [p for p in visio if p not in net and p not in recent]
        paths = []
        if net:
            paths.append(sorted(net)[-1])
        paths.extend(recent[-1:] if recent else [])
        paths.extend(other)
        report["installed"].append(
            {"kind": "hpe-aruba-visio", **install_visio_zips(paths, "hpe-aruba-visio")}
        )

    if symbols:
        report["installed"].append(
            {
                "kind": "hpe-aruba-symbols",
                **install_visio_zips(symbols, "hpe-aruba-symbols"),
            }
        )

    if pptx:
        report["installed"].append({"kind": "hpe-technical-pptx", **install_pptx(pptx)})

    if args.juniper or args.from_downloads:
        # public Juniper packs stay local/gitignored
        report["installed"].append(
            {"kind": "juniper-public", **install_juniper_public(all_photos=bool(getattr(args, "juniper_all_photos", False) or args.from_downloads))}
        )

    if not report["installed"]:
        print(
            "No packs installed. Pass --from-downloads, --pptx, --visio-zip, "
            "and/or --symbols-zip.",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(report, indent=2))
    print(
        "\nNext: enable design backend (HPE_MCP_PRODUCTS=design) and call "
        "list_diagram_icons / export_graphviz_topology with vendor=aruba.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
