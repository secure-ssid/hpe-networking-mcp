"""Parse VLAN interface (L3) definitions from a text config snippet.

Handles the following patterns (AOS-CX CLI style):

    interface vlan 5
        ip address 10.11.154.2/24
        ip helper-address 10.11.154.19

    interface vlan 33
        ip address 172.16.33.2/24

    interface vlan 50
        ip dhcp

Returns a list of dicts:
    {
        "vlan":           int,
        "ip_address":     str | None,   # "10.11.154.2/24"
        "helper_address": str | None,   # "10.11.154.19"
        "dhcp":           bool,         # True → ip dhcp (no static address)
    }
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# interface vlan 5
_INTF_VLAN_RE = re.compile(r"^interface\s+vlan\s+(\d+)", re.IGNORECASE)
# ip address 10.11.154.2/24
_IP_ADDR_RE = re.compile(r"^\s+ip\s+address\s+(\S+)", re.IGNORECASE)
# ip helper-address 10.11.154.19
_HELPER_RE = re.compile(r"^\s+ip\s+helper-address\s+(\S+)", re.IGNORECASE)
# ip dhcp
_DHCP_RE = re.compile(r"^\s+ip\s+dhcp\b", re.IGNORECASE)


def parse_vlan_interfaces(text: str) -> list[dict]:
    """Parse VLAN interface definitions from a config text block.

    Args:
        text: Raw config text (may be a full file or just the relevant stanzas).

    Returns:
        List of dicts with keys: vlan (int), ip_address (str|None),
        helper_address (str|None), dhcp (bool). Sorted by VLAN ID.
    """
    results: dict[int, dict] = {}
    current_vlan: Optional[int] = None

    for line in text.splitlines():
        m = _INTF_VLAN_RE.match(line)
        if m:
            current_vlan = int(m.group(1))
            if current_vlan not in results:
                results[current_vlan] = {
                    "vlan": current_vlan,
                    "ip_address": None,
                    "helper_address": None,
                    "dhcp": False,
                }
            continue

        if current_vlan is None:
            continue

        # Blank line or new top-level stanza ends the current block
        if line and not line[0].isspace():
            current_vlan = None
            continue

        m = _IP_ADDR_RE.match(line)
        if m:
            results[current_vlan]["ip_address"] = m.group(1)
            continue

        m = _HELPER_RE.match(line)
        if m:
            results[current_vlan]["helper_address"] = m.group(1)
            continue

        if _DHCP_RE.match(line):
            results[current_vlan]["dhcp"] = True

    return sorted(results.values(), key=lambda r: r["vlan"])


def parse_vlan_interfaces_from_file(path: str) -> list[dict]:
    """Parse VLAN interface definitions from a file.

    Args:
        path: Path to the config file or snippet.

    Returns:
        List of dicts. See parse_vlan_interfaces().

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"VLAN interface config file not found: {path}")
    text = config_path.read_text()
    result = parse_vlan_interfaces(text)
    logger.info("Parsed %d VLAN interface(s) from %s", len(result), path)
    return result


def load_vlan_interface_config_file(path: Optional[str]) -> list[dict]:
    """Wrapper that returns an empty list if path is None or blank."""
    if not path:
        return []
    return parse_vlan_interfaces_from_file(path)
