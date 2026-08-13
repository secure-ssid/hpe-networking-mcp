"""Canonical topology model for design diagram exporters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Logical roles → default shape / layer for layouts
KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "cloud",
        "internet",
        "firewall",
        "router",
        "core_switch",
        "agg_switch",
        "access_switch",
        "gateway",
        "campus_ap",
        "mist_ap",
        "clearpass",
        "controller",
        "server",
        "client",
        "generic",
    }
)

KNOWN_VENDORS: frozenset[str] = frozenset(
    {
        "generic",
        "aruba",
        "hpe",
        "clearpass",
        "mist",
        "juniper",
        "cisco",
        "axis",
        "apstra",
        "uxi",
        "edgeconnect",
    }
)

ROLE_LAYER: dict[str, int] = {
    "cloud": 0,
    "internet": 0,
    "firewall": 1,
    "router": 1,
    "core_switch": 2,
    "controller": 2,
    "gateway": 2,
    "agg_switch": 3,
    "access_switch": 4,
    "clearpass": 3,
    "server": 3,
    "campus_ap": 5,
    "mist_ap": 5,
    "client": 6,
    "generic": 4,
}


@dataclass
class DiagramNode:
    id: str
    label: str
    role: str = "generic"
    vendor: str = "generic"
    site: str | None = None
    group: str | None = None
    mgmt_ip: str | None = None
    serial: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "role": self.role,
            "vendor": self.vendor,
        }
        if self.site:
            out["site"] = self.site
        if self.group:
            out["group"] = self.group
        if self.mgmt_ip:
            out["mgmt_ip"] = self.mgmt_ip
        if self.serial:
            out["serial"] = self.serial
        if self.extra:
            out["extra"] = self.extra
        return out


@dataclass
class DiagramLink:
    source: str
    target: str
    label: str | None = None
    link_type: str = "ethernet"  # ethernet | trunk | wireless | wan | logical
    bandwidth: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "target": self.target,
            "link_type": self.link_type,
        }
        if self.label:
            out["label"] = self.label
        if self.bandwidth:
            out["bandwidth"] = self.bandwidth
        return out


@dataclass
class DiagramGroup:
    id: str
    label: str
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "members": list(self.members)}


@dataclass
class DiagramModel:
    title: str
    nodes: list[DiagramNode] = field(default_factory=list)
    links: list[DiagramLink] = field(default_factory=list)
    groups: list[DiagramGroup] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [lnk.to_dict() for lnk in self.links],
            "groups": [g.to_dict() for g in self.groups],
            "notes": list(self.notes),
        }

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


def _as_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _check_id(value: str, field_name: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field_name}={value!r} must match {_ID_RE.pattern} (1-64 safe chars)"
        )
    return value


def parse_model(raw: dict[str, Any] | DiagramModel) -> DiagramModel:
    """Parse and lightly normalize a diagram model dict."""
    if isinstance(raw, DiagramModel):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("model must be an object")

    title = _as_str(raw.get("title") or "Network design", "title")
    nodes_in = raw.get("nodes") or []
    links_in = raw.get("links") or []
    groups_in = raw.get("groups") or []
    notes_in = raw.get("notes") or []

    if not isinstance(nodes_in, list) or not nodes_in:
        raise ValueError("model.nodes must be a non-empty list")
    if len(nodes_in) > 200:
        raise ValueError("model.nodes exceeds max 200")
    if not isinstance(links_in, list):
        raise ValueError("model.links must be a list")
    if len(links_in) > 500:
        raise ValueError("model.links exceeds max 500")
    if not isinstance(groups_in, list):
        raise ValueError("model.groups must be a list")
    if len(groups_in) > 50:
        raise ValueError("model.groups exceeds max 50")

    nodes: list[DiagramNode] = []
    seen: set[str] = set()
    for i, item in enumerate(nodes_in):
        if not isinstance(item, dict):
            raise ValueError(f"nodes[{i}] must be an object")
        nid = _check_id(_as_str(item.get("id"), f"nodes[{i}].id"), f"nodes[{i}].id")
        if nid in seen:
            raise ValueError(f"duplicate node id {nid!r}")
        seen.add(nid)
        label = _as_str(item.get("label") or nid, f"nodes[{i}].label")
        role = str(item.get("role") or "generic").strip().lower()
        if role not in KNOWN_ROLES:
            role = "generic"
        vendor = str(item.get("vendor") or "generic").strip().lower()
        if vendor not in KNOWN_VENDORS:
            vendor = "generic"
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        nodes.append(
            DiagramNode(
                id=nid,
                label=label[:80],
                role=role,
                vendor=vendor,
                site=(str(item["site"]).strip()[:80] if item.get("site") else None),
                group=(str(item["group"]).strip()[:64] if item.get("group") else None),
                mgmt_ip=(str(item["mgmt_ip"]).strip()[:64] if item.get("mgmt_ip") else None),
                serial=(str(item["serial"]).strip()[:64] if item.get("serial") else None),
                extra=extra,
            )
        )

    id_set = {n.id for n in nodes}
    links: list[DiagramLink] = []
    for i, item in enumerate(links_in):
        if not isinstance(item, dict):
            raise ValueError(f"links[{i}] must be an object")
        src = _check_id(_as_str(item.get("source"), f"links[{i}].source"), f"links[{i}].source")
        dst = _check_id(_as_str(item.get("target"), f"links[{i}].target"), f"links[{i}].target")
        if src not in id_set or dst not in id_set:
            raise ValueError(f"links[{i}] references unknown node ({src} -> {dst})")
        if src == dst:
            raise ValueError(f"links[{i}] cannot be self-loop")
        lt = str(item.get("link_type") or "ethernet").strip().lower()
        if lt not in {"ethernet", "trunk", "wireless", "wan", "logical"}:
            lt = "ethernet"
        links.append(
            DiagramLink(
                source=src,
                target=dst,
                label=(str(item["label"]).strip()[:40] if item.get("label") else None),
                link_type=lt,
                bandwidth=(
                    str(item["bandwidth"]).strip()[:20] if item.get("bandwidth") else None
                ),
            )
        )

    groups: list[DiagramGroup] = []
    for i, item in enumerate(groups_in):
        if not isinstance(item, dict):
            raise ValueError(f"groups[{i}] must be an object")
        gid = _check_id(_as_str(item.get("id"), f"groups[{i}].id"), f"groups[{i}].id")
        label = _as_str(item.get("label") or gid, f"groups[{i}].label")
        members_raw = item.get("members") or []
        if not isinstance(members_raw, list):
            raise ValueError(f"groups[{i}].members must be a list")
        members = []
        for m in members_raw:
            mid = _check_id(str(m).strip(), f"groups[{i}].members")
            if mid not in id_set:
                raise ValueError(f"groups[{i}] unknown member {mid!r}")
            members.append(mid)
        groups.append(DiagramGroup(id=gid, label=label[:80], members=members))

    notes: list[str] = []
    if isinstance(notes_in, list):
        for n in notes_in[:20]:
            if isinstance(n, str) and n.strip():
                notes.append(n.strip()[:200])

    return DiagramModel(title=title[:120], nodes=nodes, links=links, groups=groups, notes=notes)


def validate_model(raw: dict[str, Any] | DiagramModel) -> dict[str, Any]:
    """Validate a model; return ok + normalized model or errors."""
    try:
        model = parse_model(raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "model": model.to_dict(),
        "counts": {
            "nodes": len(model.nodes),
            "links": len(model.links),
            "groups": len(model.groups),
        },
        "known_roles": sorted(KNOWN_ROLES),
        "known_vendors": sorted(KNOWN_VENDORS),
    }


def model_from_central_topology(
    topology: dict[str, Any],
    *,
    title: str | None = None,
    site_id: str | None = None,
) -> DiagramModel:
    """Best-effort conversion from Central ``get_topology``-shaped payloads.

    Accepts flexible node/link key names used across Central topology responses.
    """
    if not isinstance(topology, dict):
        raise ValueError("topology must be an object")

    nodes_raw = (
        topology.get("nodes")
        or topology.get("devices")
        or topology.get("vertices")
        or []
    )
    links_raw = (
        topology.get("links")
        or topology.get("edges")
        or topology.get("connections")
        or []
    )
    if not isinstance(nodes_raw, list) or not nodes_raw:
        # Some envelopes nest under data
        data = topology.get("data") if isinstance(topology.get("data"), dict) else {}
        nodes_raw = data.get("nodes") or data.get("devices") or []
        links_raw = data.get("links") or data.get("edges") or []

    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError("topology payload has no nodes/devices")

    nodes: list[dict[str, Any]] = []
    for i, item in enumerate(nodes_raw[:200]):
        if not isinstance(item, dict):
            continue
        nid = str(
            item.get("id")
            or item.get("device_id")
            or item.get("serial")
            or item.get("name")
            or f"node_{i}"
        )
        nid = re.sub(r"[^A-Za-z0-9_.:-]", "_", nid)[:64]
        label = str(item.get("name") or item.get("hostname") or item.get("label") or nid)
        dtype = str(
            item.get("type")
            or item.get("device_type")
            or item.get("role")
            or item.get("persona")
            or "generic"
        ).lower()
        role = _map_central_type(dtype)
        vendor = "aruba"
        if "mist" in dtype:
            vendor = "mist"
        nodes.append(
            {
                "id": nid,
                "label": label,
                "role": role,
                "vendor": vendor,
                "site": site_id or item.get("site_id") or item.get("site"),
                "serial": item.get("serial") or item.get("serial_number"),
                "mgmt_ip": item.get("ip") or item.get("mgmt_ip") or item.get("ipv4"),
            }
        )

    id_set = {n["id"] for n in nodes}
    # remap original ids if sanitization changed them — rebuild from nodes list order
    links: list[dict[str, Any]] = []
    if isinstance(links_raw, list):
        for item in links_raw[:500]:
            if not isinstance(item, dict):
                continue
            src = str(
                item.get("source")
                or item.get("from")
                or item.get("src")
                or item.get("local_device")
                or ""
            )
            dst = str(
                item.get("target")
                or item.get("to")
                or item.get("dst")
                or item.get("remote_device")
                or ""
            )
            src = re.sub(r"[^A-Za-z0-9_.:-]", "_", src)[:64]
            dst = re.sub(r"[^A-Za-z0-9_.:-]", "_", dst)[:64]
            if src in id_set and dst in id_set and src != dst:
                links.append(
                    {
                        "source": src,
                        "target": dst,
                        "label": item.get("label") or item.get("name"),
                        "link_type": "wireless"
                        if "wireless" in str(item.get("type", "")).lower()
                        else "ethernet",
                    }
                )

    return parse_model(
        {
            "title": title or f"Topology {site_id or ''}".strip() or "Central topology",
            "nodes": nodes,
            "links": links,
            "notes": ["Converted from Central topology payload"],
        }
    )


def _map_central_type(dtype: str) -> str:
    d = dtype.lower()
    if any(x in d for x in ("ap", "access_point", "campus_ap", "iap")):
        return "campus_ap"
    if "mist" in d:
        return "mist_ap"
    if any(x in d for x in ("gateway", "gw", "branch_gw", "mobility")):
        return "gateway"
    if any(x in d for x in ("core",)):
        return "core_switch"
    if any(x in d for x in ("agg", "aggregation", "dist")):
        return "agg_switch"
    if any(x in d for x in ("switch", "cx", "aos-s", "access_switch")):
        return "access_switch"
    if "firewall" in d or "fw" == d:
        return "firewall"
    if "router" in d:
        return "router"
    if "clearpass" in d or "cppm" in d:
        return "clearpass"
    if "controller" in d:
        return "controller"
    return "generic"


def layout_positions(model: DiagramModel, *, col_width: int = 180, row_height: int = 120) -> dict[str, tuple[int, int]]:
    """Simple layered grid layout (x, y) for Draw.io / Graphviz positions."""
    layers: dict[int, list[DiagramNode]] = {}
    for node in model.nodes:
        layer = ROLE_LAYER.get(node.role, 4)
        layers.setdefault(layer, []).append(node)

    positions: dict[str, tuple[int, int]] = {}
    for layer in sorted(layers):
        row = layers[layer]
        for i, node in enumerate(row):
            x = 80 + i * col_width
            y = 80 + layer * row_height
            positions[node.id] = (x, y)
    return positions
