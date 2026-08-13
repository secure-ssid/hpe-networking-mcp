"""Parse VLAN definitions from an AOS 8 controller configuration file.

Handles two AOS 8 config patterns:

  Pattern A — vlan declaration block:
      vlan 17
      vlan 18
      ...

  Pattern B — vlan-name/vlan name binding:
      vlan-name SEW_Micros_Tablets
      vlan SEW_Micros_Tablets 17

Returns a list of dicts: [{"vlan": int, "name": str, "enable": True}, ...]
VLANs 1 and 4094 are always excluded (reserved).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_RESERVED_VLANS = {1, 4094}

# Matches:  vlan-name SEW_Micros_Tablets
_VLAN_NAME_RE = re.compile(r"^vlan-name\s+(\S+)", re.MULTILINE)

# Matches:  vlan SEW_Micros_Tablets 17   (name-to-id binding)
_VLAN_BIND_RE = re.compile(r"^vlan\s+(\S+)\s+(\d+)", re.MULTILINE)

# Matches:  vlan 17   (bare numeric declaration, no name on same line)
_VLAN_NUM_RE = re.compile(r"^vlan\s+(\d+)\s*$", re.MULTILINE)


def parse_vlans_from_aos8_config(path: str) -> list[dict]:
    """Parse VLAN IDs and names from an AOS 8 config file.

    Args:
        path: Path to the AOS 8 config file.

    Returns:
        List of dicts with keys: vlan (int), name (str), enable (bool).
        Sorted by VLAN ID. Reserved VLANs (1, 4094) are excluded.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"VLAN config file not found: {path}")

    text = config_path.read_text()

    # Build name → vlan_id map from "vlan-name NAME" + "vlan NAME ID" pairs
    name_to_id: dict[str, int] = {}
    for m in _VLAN_BIND_RE.finditer(text):
        name, vlan_id_str = m.group(1), m.group(2)
        vlan_id = int(vlan_id_str)
        if vlan_id not in _RESERVED_VLANS:
            name_to_id[name] = vlan_id

    # Collect all numeric VLAN IDs declared in the config
    numeric_ids: set[int] = set()
    for m in _VLAN_NUM_RE.finditer(text):
        vlan_id = int(m.group(1))
        if vlan_id not in _RESERVED_VLANS:
            numeric_ids.add(vlan_id)

    # Build id → name map (invert name_to_id)
    id_to_name: dict[int, str] = {v: k for k, v in name_to_id.items()}

    # Union of all VLAN IDs found
    all_ids = numeric_ids | set(name_to_id.values())

    vlans = []
    for vlan_id in sorted(all_ids):
        name = id_to_name.get(vlan_id, str(vlan_id))
        vlans.append({"vlan": vlan_id, "name": name, "enable": True})

    logger.info("Parsed %d VLANs from %s", len(vlans), path)
    return vlans


def load_vlan_config_file(path: Optional[str]) -> list[dict]:
    """Wrapper that returns an empty list if path is None or blank."""
    if not path:
        return []
    return parse_vlans_from_aos8_config(path)
