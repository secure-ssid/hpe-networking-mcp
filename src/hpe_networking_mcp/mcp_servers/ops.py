"""MCP server — Aruba Central ops: troubleshooting and device actions (41 tools).

Covers: CX/AOS-S/Gateway/AP ping/traceroute/show, PoE bounce, port bounce, cable test,
reboot, disconnect client, acknowledge alert, LLDP neighbors, ARP table, MAC table,
speed test, find MAC on switch, port error counters, spanning tree, interface counters,
device notes update/delete, gateway iperf/ping-sweep/halt, AP tcp/nslookup/http/https,
show-command catalog validation, locate operations (AP/CX/AOS-S), destructive AP-swarm
reboot, best-effort CX stack-conductor serial resolution, and a bounded
CX/AOS-S troubleshooting orchestration bundle (run_troubleshooting_bundle:
LLDP/ARP/ping/show composed from the existing per-op tools, partial-failure
safe).
"""
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel

from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    DIAGNOSTIC,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    atroubleshoot_async,
    compact_http_error,
    device_type_for_troubleshoot,
    get_client,
    get_mcp_client,
    resp_json,
    troubleshooting_endpoint_candidates,
)

mcp = MCPServer("central-ops")


class _ConfirmAction(BaseModel):
    confirm: bool = False


async def _arequest_troubleshooting(
    method: str,
    segment: str,
    serial_number: str,
    action: str,
    *,
    diagnostic: bool = False,
    **kwargs: Any,
) -> tuple[Any, str]:
    candidates = troubleshooting_endpoint_candidates(segment, serial_number, action)
    client = get_client()
    for index, endpoint in enumerate(candidates):
        response = await client._arequest(
            method,
            endpoint,
            diagnostic=diagnostic,
            **kwargs,
        )
        if response.status_code != 404 or index == len(candidates) - 1:
            return response, endpoint
    raise RuntimeError("no troubleshooting endpoint candidates provided")


def _request_troubleshooting(
    method: str,
    segment: str,
    serial_number: str,
    action: str,
    **kwargs: Any,
) -> tuple[Any, str]:
    candidates = troubleshooting_endpoint_candidates(segment, serial_number, action)
    client = get_client()
    for index, endpoint in enumerate(candidates):
        response = client._request(method, endpoint, **kwargs)
        if response.status_code != 404 or index == len(candidates) - 1:
            return response, endpoint
    raise RuntimeError("no troubleshooting endpoint candidates provided")


async def _cx_show_commands(serial_number: str, commands: list[str]) -> dict[str, Any]:
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("cx", serial_number, "showCommands"),
        {"commands": commands},
        errors,
        diagnostic=True,
    )


# ── CX Troubleshooting ────────────────────────────────────────────────────────

@mcp.tool(annotations=DIAGNOSTIC)
async def cx_ping(
    serial_number: str,
    destination: str,
    count: int | None = None,
    packet_size: int | None = None,
    vrf_name: str | None = None,
    use_management_interface: bool | None = None,
) -> dict[str, Any]:
    """Ping a destination from a CX switch and return the result (async, polls ~60s)."""
    client = get_client()
    errors: list[str] = []
    payload: dict[str, Any] = {"destination": destination}
    if count is not None:
        payload["count"] = count
    if packet_size is not None:
        payload["packetSize"] = packet_size
    if vrf_name is not None:
        payload["vrfName"] = vrf_name
    if use_management_interface is not None:
        payload["useManagementInterface"] = use_management_interface

    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("cx", serial_number, "ping"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def cx_traceroute(
    serial_number: str,
    destination: str,
    vrf_name: str | None = None,
    use_management_interface: bool | None = None,
) -> dict[str, Any]:
    """Run a traceroute from a CX switch (async, polls ~60s)."""
    client = get_client()
    errors: list[str] = []
    payload: dict[str, Any] = {"destination": destination}
    if vrf_name is not None:
        payload["vrfName"] = vrf_name
    if use_management_interface is not None:
        payload["useManagementInterface"] = use_management_interface

    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("cx", serial_number, "traceroute"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def cx_show(
    serial_number: str,
    commands: list[str],
) -> dict[str, Any]:
    """Run 'show' commands on a CX switch (all must start with 'show ', max 20, async polls ~60s)."""
    if not commands:
        return {"status": None, "errors": ["commands list cannot be empty"]}
    if len(commands) > 20:
        return {"status": None, "errors": [f"commands list cannot exceed 20 items (got {len(commands)})"]}
    for i, cmd in enumerate(commands):
        if not cmd.strip().lower().startswith("show "):
            return {"status": None, "errors": [f"Command {i} must start with 'show ': '{cmd}'"]}

    return await _cx_show_commands(serial_number, commands)


# ── AOS-S Troubleshooting ─────────────────────────────────────────────────────

@mcp.tool(annotations=DIAGNOSTIC)
async def aos_s_ping(serial_number: str, destination: str) -> dict[str, Any]:
    """Ping a destination from an AOS-S switch (async, polls ~60s)."""
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("aos-s", serial_number, "ping"),
        {"destination": destination},
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def aos_s_traceroute(serial_number: str, destination: str) -> dict[str, Any]:
    """Run a traceroute from an AOS-S switch (async, polls ~60s)."""
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("aos-s", serial_number, "traceroute"),
        {"destination": destination},
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def aos_s_show(serial_number: str, commands: list[str]) -> dict[str, Any]:
    """Run 'show' commands on an AOS-S switch (all must start with 'show ', async polls ~60s)."""
    if not commands:
        return {"status": None, "errors": ["commands list cannot be empty"]}
    for i, cmd in enumerate(commands):
        if not cmd.strip().lower().startswith("show "):
            return {"status": None, "errors": [f"Command {i} must start with 'show ': '{cmd}'"]}
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("aos-s", serial_number, "showCommands"),
        {"commands": commands},
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def gateway_show(serial_number: str, commands: list[str]) -> dict[str, Any]:
    """Run 'show' commands on an Aruba gateway via async troubleshooting API. Each must start with 'show '."""
    if not commands:
        return {"status": None, "errors": ["commands list cannot be empty"]}
    for i, cmd in enumerate(commands):
        if not cmd.strip().lower().startswith("show "):
            return {"status": None, "errors": [f"Command {i} must start with 'show ': '{cmd}'"]}
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("gateways", serial_number, "showCommands"),
        {"commands": commands},
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def aos_s_arp(serial_number: str) -> dict[str, Any]:
    """Get the ARP table from an AOS-S switch (async, polls ~60s)."""
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("aos-s", serial_number, "getArpTable"),
        {},
        errors,
        diagnostic=True,
    )


# aos_s_locate was removed: Central's troubleshooting mapping only supports
# `locate` on cx / aps / gateways, not aos-s. Hitting /network-troubleshooting/v1alpha1/
# aos-s/{serial}/locate always returns "Device not found" — it's not a
# real endpoint.
#
# If AOS-S locate is needed in the future, route it differently (CLI over
# the show-commands path, or a classic-central /network-actions call).


# ── PoE / Port / Cable Ops ────────────────────────────────────────────────────

@mcp.tool(annotations=DESTRUCTIVE)
async def poe_bounce(
    ctx: Context,
    serial_number: str,
    ports: list[str],
    device_type: str | None = None,
) -> dict[str, Any]:
    """Power-cycle PoE on switch/gateway ports (async, polls ~60s).

    ports format: CX "1/1/1", AOS-S "1", Gateway "GE 0/0/0". device_type auto-detected.
    """
    errors: list[str] = []
    dtype = device_type_for_troubleshoot(serial_number, device_type)
    if dtype is None:
        errors.append(
            f"Could not determine device type for {serial_number}. "
            "Provide device_type explicitly (SWITCH/GATEWAY)."
        )
        return {"status": None, "errors": errors}
    if dtype == "aps":
        errors.append("PoE bounce is not supported on Access Points.")
        return {"status": None, "errors": errors}

    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm PoE BOUNCE on {serial_number} ports {ports}? Connected devices will temporarily lose power.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}

    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates(dtype, serial_number, "poeBounce"),
        {"ports": ports},
        errors,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def port_bounce(
    ctx: Context,
    serial_number: str,
    ports: list[str],
    device_type: str | None = None,
) -> dict[str, Any]:
    """Link-reset (bounce) switch/gateway ports (async, polls ~60s).

    ports format: CX "1/1/1", AOS-S "1", Gateway "GE 0/0/0". device_type auto-detected.
    """
    errors: list[str] = []
    dtype = device_type_for_troubleshoot(serial_number, device_type)
    if dtype is None:
        errors.append(
            f"Could not determine device type for {serial_number}. "
            "Provide device_type explicitly (SWITCH/GATEWAY)."
        )
        return {"status": None, "errors": errors}
    if dtype == "aps":
        errors.append("Port bounce is not supported on Access Points.")
        return {"status": None, "errors": errors}

    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm PORT BOUNCE on {serial_number} ports {ports}? Connected devices will lose connectivity.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}

    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates(dtype, serial_number, "portBounce"),
        {"ports": ports},
        errors,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def cable_test(
    serial_number: str,
    ports: list[str],
    device_type: str | None = None,
) -> dict[str, Any]:
    """Run a cable/TDR test on CX or AOS-S switch ports (async, polls ~60s)."""
    errors: list[str] = []
    dtype = device_type_for_troubleshoot(serial_number, device_type)
    if dtype is None:
        errors.append(
            f"Could not determine device type for {serial_number}. "
            "Provide device_type explicitly (SWITCH/GATEWAY)."
        )
        return {"status": None, "errors": errors}
    if dtype == "gateways":
        errors.append("Cable test is not supported on gateways.")
        return {"status": None, "errors": errors}
    if dtype == "aps":
        errors.append("Cable test is not supported on Access Points.")
        return {"status": None, "errors": errors}
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates(dtype, serial_number, "cableTest"),
        {"ports": ports},
        errors,
        diagnostic=True,
    )


# ── Device Actions ────────────────────────────────────────────────────────────

@mcp.tool(annotations=DESTRUCTIVE)
async def reboot_device(
    ctx: Context,
    serial_number: str,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Reboot an AP, CX switch, AOS-S switch, or gateway. device_type auto-detected if omitted."""
    errors: list[str] = []

    if not device_type:
        device = get_mcp_client().get_device_by_serial(serial_number)
        if device:
            raw = device.get("deviceType", "")
            if "ACCESS_POINT" in raw or raw == "AP":
                device_type = "AP"
            elif "SWITCH" in raw:
                device_type = "SWITCH"
            elif "GATEWAY" in raw:
                device_type = "GATEWAY"
        if not device_type:
            errors.append(f"Could not determine device type for {serial_number}. Provide device_type explicitly.")
            return {"serial_number": serial_number, "device_type": None, "response": None, "errors": errors}

    dt = device_type.upper()
    if dt in ("AP", "ACCESS_POINT"):
        segment = "aps"
    elif dt in ("CX", "SWITCH"):
        segment = "cx"
    elif dt in ("AOS-S", "AOSS", "AOS_S"):
        segment = "aos-s"
    elif dt in ("GATEWAY", "GW"):
        segment = "gateways"
    else:
        errors.append(f"Unknown device_type '{device_type}'. Use 'AP', 'CX', 'AOS-S', or 'GATEWAY'.")
        return {"serial_number": serial_number, "device_type": device_type, "response": None, "errors": errors}

    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm REBOOT of {device_type} {serial_number}? This will cause a service interruption.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}

    response = await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates(segment, serial_number, "reboot"),
        {},
        errors,
    )
    return {
        "serial_number": serial_number,
        "device_type": device_type,
        "response": response,
        "errors": response.get("errors", errors),
    }


@mcp.tool(annotations=DESTRUCTIVE)
async def disconnect_client(
    ctx: Context,
    mac_address: str,
    ap_serial: str | None = None,
) -> dict[str, Any]:
    """Force-disconnect a wireless client by MAC address. ap_serial auto-looked up if omitted."""
    client = get_client()
    errors: list[str] = []

    # Resolve AP serial if not provided
    if not ap_serial:
        cl = get_mcp_client().find_client(mac_address)
        if not cl:
            return {"mac_address": mac_address, "response": None, "errors": ["Client not found in monitoring"]}
        ap_serial = cl.get("connectedDeviceSerial")
        if not ap_serial:
            return {"mac_address": mac_address, "response": None, "errors": ["Could not determine connected AP serial"]}

    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm DISCONNECT of client {mac_address}? The client will be forced off the network.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}

    response = await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates(
            "aps", ap_serial, "disconnectUserByMacAddress"
        ),
        {"userMacAddress": mac_address},
        errors,
    )
    return {
        "mac_address": mac_address,
        "ap_serial": ap_serial,
        "endpoint_used": response.get("endpoint_used"),
        "response": response,
        "errors": response.get("errors", errors),
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def acknowledge_alert(
    alert_id: str,
    action: str = "ACK",
) -> dict[str, Any]:
    """Acknowledge, clear, or resolve an active alert. action: ACK/CLEAR/RESOLVE.

    KNOWN ISSUE (2026-04): all candidate paths 404 on this tenant — no peer MCP
    wraps this either. Tool preserved for structured 'not available' response.
    """
    client = get_client()
    errors: list[str] = []

    candidates = [
        ("POST", "/network-notifications/v1/alerts/acknowledge", {"alert_id": [alert_id], "action": action}),
        ("POST", f"/network-notifications/v1/alerts/{alert_id}/acknowledge", {"action": action}),
        ("PATCH", f"/network-notifications/v1/alerts/{alert_id}", {"status": action}),
    ]

    for method, endpoint, payload in candidates:
        try:
            response = client._request(method, endpoint, json=payload)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(compact_http_error(response, endpoint=endpoint))
                continue
            try:
                resp_body = response.json()
            except Exception:
                resp_body = {}
            return {"alert_id": alert_id, "action": action, "endpoint_used": endpoint, "response": resp_body, "errors": errors}
        except Exception as exc:
            errors.append(str(exc))

    errors.append(
        "acknowledge_alert: no candidate path accepted the request. "
        "This endpoint may not be exposed on New Central; track "
        "https://developer.arubanetworks.com/new-central/reference for updates."
    )
    return {"alert_id": alert_id, "action": action, "response": None, "errors": errors}


# ── CX Switch Intelligence ────────────────────────────────────────────────────

@mcp.tool(annotations=DIAGNOSTIC)
async def get_lldp_neighbors(serial_number: str) -> dict[str, Any]:
    """Get LLDP neighbor table from a CX switch.

    Shows what device is connected to each port — hostname, port ID, system
    capabilities, and management address. Useful for instantly identifying what's
    plugged into a port without guessing from MAC or client data.
    """
    return await _cx_show_commands(serial_number, ["show lldp neighbors"])


@mcp.tool(annotations=DIAGNOSTIC)
async def get_cx_arp_table(serial_number: str) -> dict[str, Any]:
    """Get the ARP table from a CX switch.

    Returns IP-to-MAC mappings with interface and VLAN. Useful for resolving
    an IP to a MAC when a client doesn't appear in Central's client list.
    """
    return await _cx_show_commands(serial_number, ["show arp"])


@mcp.tool(annotations=DIAGNOSTIC)
async def get_cx_mac_table(
    serial_number: str,
    interface: str | None = None,
) -> dict[str, Any]:
    """Get the MAC address table from a CX switch.

    Shows which MAC addresses are learned on which ports and VLANs. When
    interface is provided (e.g. '1/1/16'), filters to that port only.
    Useful for tracing exactly which port a device is connected to.
    """
    cmd = f"show mac-address-table interface {interface}" if interface else "show mac-address-table"
    return await _cx_show_commands(serial_number, [cmd])


@mcp.tool(annotations=DIAGNOSTIC)
async def find_mac_on_switch(serial_number: str, mac_address: str) -> dict[str, Any]:
    """Find which port a MAC address is learned on for a CX switch.

    Runs 'show mac-address-table address <mac>' and returns the port, VLAN,
    and entry type. The fastest way to answer "what port is device X on?"
    """
    mac_clean = mac_address.replace("-", ":").lower()
    result = await _cx_show_commands(serial_number, [f"show mac-address-table address {mac_clean}"])
    result["mac_address"] = mac_address
    return result


@mcp.tool(annotations=DIAGNOSTIC)
async def get_switch_port_errors(serial_number: str, interface: str | None = None) -> dict[str, Any]:
    """Get error counters for CX switch ports.

    Returns CRC errors, input errors, output errors, runts, giants, and
    collisions. When interface is given (e.g. '1/1/5') only that port is
    queried; otherwise all interfaces. First thing to check for a flapping
    or slow port.
    """
    cmd = (
        f"show interface {interface} statistics"
        if interface
        else "show interface statistics"
    )
    return await _cx_show_commands(serial_number, [cmd])


@mcp.tool(annotations=DIAGNOSTIC)
async def get_switch_spanning_tree(
    serial_number: str,
    interface: str | None = None,
) -> dict[str, Any]:
    """Get spanning tree topology for a CX switch.

    Returns bridge ID, root bridge, port roles (Root/Designated/Alternate/
    Backup), port states (Forwarding/Blocking/Learning), and timers.
    When interface is provided only that port's STP detail is returned.
    Essential for diagnosing broadcast storms, topology changes, and loops.
    """
    cmd = (
        f"show spanning-tree detail interface {interface}"
        if interface
        else "show spanning-tree detail"
    )
    return await _cx_show_commands(serial_number, [cmd])


@mcp.tool(annotations=DIAGNOSTIC)
async def get_switch_interface_counters(
    serial_number: str,
    interface: str | None = None,
) -> dict[str, Any]:
    """Get Tx/Rx byte and packet counters for CX switch interfaces.

    Returns transmitted/received bytes, unicast/multicast/broadcast packet
    counts and rates. When interface is provided (e.g. '1/1/1') only that
    port is returned. Use for capacity analysis and saturation detection.
    """
    cmd = (
        f"show interface {interface} counters"
        if interface
        else "show interface counters"
    )
    return await _cx_show_commands(serial_number, [cmd])


@mcp.tool(annotations=DIAGNOSTIC)
async def run_speed_test(serial_number: str) -> dict[str, Any]:
    """Run a speed test from an AP to measure uplink bandwidth.

    Uses the Central async troubleshooting API. Returns download/upload
    throughput and latency from the AP's perspective. Useful for verifying
    whether a slow client experience is a radio issue or an uplink issue.
    """
    client = get_client()
    errors: list[str] = []
    return await atroubleshoot_async(
        client,
        troubleshooting_endpoint_candidates("aps", serial_number, "speedtest"),
        {},
        errors,
        diagnostic=True,
    )


# ── Troubleshooting Orchestration (v0.7 bounded workflow) ────────────────────
#
# Composes the existing, individually-confirmed CX/AOS-S troubleshooting
# tools above (get_lldp_neighbors, get_cx_arp_table, aos_s_arp, cx_ping/
# aos_s_ping, cx_show/aos_s_show) into one bounded diagnostic bundle. No new
# endpoint is introduced — every step below reuses an already-implemented,
# manifest-backed tool call. Each step runs independently and its own
# failure is captured rather than aborting the remaining steps (partial
# failure never surfaces as a whole-bundle exception). Read-only/diagnostic
# by nature (ping/traceroute/show/LLDP/ARP queries), so — consistent with
# the individual tools it composes — this does not gate behind dry_run or
# the Central write gate.

_TROUBLESHOOTING_BUNDLE_DEVICE_TYPES = ("cx", "aos-s")
_MAX_BUNDLE_SHOW_COMMANDS = 5


@mcp.tool(annotations=DIAGNOSTIC)
async def run_troubleshooting_bundle(
    serial_number: str,
    device_type: str,
    destination: str | None = None,
    commands: list[str] | None = None,
) -> dict[str, Any]:
    """Run a bounded LLDP/ARP/ping/show diagnostic bundle against one CX or AOS-S switch.

    device_type must be "cx" or "aos-s". Always runs an ARP-table step
    (CX: 'show arp' via get_cx_arp_table; AOS-S: aos_s_arp); CX additionally
    runs an LLDP-neighbors step (get_lldp_neighbors). destination is
    optional — when given, adds a ping step (cx_ping/aos_s_ping). commands
    is optional — when given (each must start with 'show ', max 5), adds a
    show-command step (cx_show/aos_s_show). Every step is independent: one
    step's failure is captured in that step's "error" and does not stop the
    remaining steps from running (result["steps"] always has one entry per
    attempted step; check result["failed_steps"] for a bounded failure
    count).
    """
    dtype = device_type.strip().lower()
    if dtype not in _TROUBLESHOOTING_BUNDLE_DEVICE_TYPES:
        raise ValueError(
            f"device_type must be one of {_TROUBLESHOOTING_BUNDLE_DEVICE_TYPES}"
        )
    if commands is not None:
        if len(commands) > _MAX_BUNDLE_SHOW_COMMANDS:
            raise ValueError(
                f"commands cannot exceed {_MAX_BUNDLE_SHOW_COMMANDS} entries (got {len(commands)})"
            )
        for i, cmd in enumerate(commands):
            if not cmd.strip().lower().startswith("show "):
                raise ValueError(f"commands[{i}] must start with 'show ': '{cmd}'")

    steps: list[dict[str, Any]] = []

    async def _run_step(name: str, coro: Any) -> None:
        try:
            data = await coro
            error: str | None = None
            if isinstance(data, dict):
                result_status = str(data.get("status") or "").upper()
                if result_status in {"ERROR", "FAILED", "FAILURE"}:
                    error = str(data.get("error") or f"backend status {result_status}")
                elif data.get("errors"):
                    error = str(data["errors"])
                elif data.get("error"):
                    error = str(data["error"])
            if error:
                steps.append(
                    {
                        "name": name,
                        "status": "error",
                        "error": error[:1000],
                        "result": data,
                    }
                )
            else:
                steps.append({"name": name, "status": "ok", "result": data})
        except Exception as exc:
            steps.append({"name": name, "status": "error", "error": str(exc)[:1000]})

    if dtype == "cx":
        await _run_step("lldp_neighbors", get_lldp_neighbors(serial_number))
        await _run_step("arp_table", get_cx_arp_table(serial_number))
        if destination:
            await _run_step("ping", cx_ping(serial_number, destination))
        if commands:
            await _run_step("show", cx_show(serial_number, commands))
    else:
        await _run_step("arp_table", aos_s_arp(serial_number))
        if destination:
            await _run_step("ping", aos_s_ping(serial_number, destination))
        if commands:
            await _run_step("show", aos_s_show(serial_number, commands))

    failed = [s["name"] for s in steps if s["status"] == "error"]
    return {
        "serial_number": serial_number,
        "device_type": dtype,
        "steps": steps,
        "step_count": len(steps),
        "failed_steps": failed,
    }


# ── Device Notes ──────────────────────────────────────────────────────────────
#
# PATCH /network-monitoring/v1/devices/{serial-number} — confirmed via
# updatedevicenotesv1: "To delete the notes, please set the notes to empty
# string." delete_device_notes is therefore update_device_notes(notes="").

_DEVICE_NOTES_MAX_CHARS = 256


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_device_notes(
    serial_number: str,
    notes: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Set free-text notes on a device by serial number (max 256 chars).

    PATCH /network-monitoring/v1/devices/{serial-number}. Pass notes="" (or
    use delete_device_notes) to clear existing notes.
    """
    if len(notes) > _DEVICE_NOTES_MAX_CHARS:
        raise ValueError(f"notes must be at most {_DEVICE_NOTES_MAX_CHARS} characters")
    endpoint = f"/network-monitoring/v1/devices/{quote(serial_number, safe='')}"
    payload = {"notes": notes}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    response = get_client()._request("PATCH", endpoint, json=payload)
    if response.status_code not in (200, 201, 202, 204):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return resp_json(response) | {"endpoint_used": endpoint, "serial_number": serial_number}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def delete_device_notes(serial_number: str, dry_run: bool = False) -> dict[str, Any]:
    """Clear notes on a device by serial number (sets notes to an empty string)."""
    return update_device_notes(serial_number, "", dry_run=dry_run)


# ── Gateway Diagnostics: iperf / ping-sweep / halt ───────────────────────────

@mcp.tool(annotations=DIAGNOSTIC)
async def gateway_iperf(
    serial_number: str,
    iperf_server_address: str,
    port: int | None = None,
    duration: int | None = None,
    parallel: int | None = None,
    omit: int | None = None,
    include_reverse: bool | None = None,
    vlan_interface: str | None = None,
    protocol: str | None = None,
    include_raw_output: bool | None = None,
) -> dict[str, Any]:
    """Run an iperf throughput test from an Aruba gateway (async, polls ~60s).

    port: TCP port 1-65535. duration: seconds 10-120. protocol: tcp or udp.
    """
    payload: dict[str, Any] = {"iperfServerAddress": iperf_server_address}
    for key, value in (
        ("port", port), ("duration", duration), ("parallel", parallel), ("omit", omit),
        ("includeReverse", include_reverse), ("vlanInterface", vlan_interface),
        ("protocol", protocol), ("includeRawOutput", include_raw_output),
    ):
        if value is not None:
            payload[key] = value
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("gateways", serial_number, "iperf"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def gateway_ping_sweep(
    serial_number: str,
    destination: str,
    start_packet_size: int,
    end_packet_size: int,
    sweep_interval: int,
    count: int = 5,
) -> dict[str, Any]:
    """Run a ping sweep (a range of packet sizes) from an Aruba gateway (async, polls ~60s).

    start_packet_size/end_packet_size: sweep range in bytes (end >= start).
    sweep_interval: step size in bytes between successive payload sizes.
    """
    if end_packet_size < start_packet_size:
        raise ValueError("end_packet_size must be >= start_packet_size")
    payload = {
        "destination": destination,
        "count": count,
        "startPacketSize": start_packet_size,
        "endPacketSize": end_packet_size,
        "sweepInterval": sweep_interval,
    }
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("gateways", serial_number, "pingSweep"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def gateway_halt(ctx: Context, serial_number: str, dry_run: bool = False) -> dict[str, Any]:
    """Halt an Aruba gateway (POST .../halt). Requires elicited confirmation — this stops
    the gateway's data plane and is far more disruptive than a reboot recovery cycle."""
    endpoints = troubleshooting_endpoint_candidates("gateways", serial_number, "halt")
    if dry_run:
        return {"dry_run": True, "endpoint": endpoints[0]}
    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm HALT of gateway {serial_number}? This stops the gateway "
            "entirely — traffic through it will drop until it is manually restarted.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}
    response, endpoint = await _arequest_troubleshooting(
        "POST", "gateways", serial_number, "halt", json={}
    )
    if response.status_code not in (200, 201, 202):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return resp_json(response) | {"endpoint_used": endpoint}


@mcp.tool(annotations=DESTRUCTIVE)
async def reboot_ap_swarm(
    ctx: Context, serial_number: str, dry_run: bool = False
) -> dict[str, Any]:
    """Reboot an entire AP swarm/cluster via one member's serial (POST .../rebootSwarm).

    Requires elicited confirmation — this reboots every AP in the swarm, not
    just the named one.
    """
    endpoints = troubleshooting_endpoint_candidates("aps", serial_number, "rebootSwarm")
    if dry_run:
        return {"dry_run": True, "endpoint": endpoints[0]}
    try:
        result = await ctx.elicit(
            message=f"⚠️ Confirm REBOOT SWARM via AP {serial_number}? This reboots every AP "
            "in the swarm/cluster, not just this one.",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {"status": "CONFIRMATION_UNAVAILABLE", "error": f"client does not support elicitation; operation NOT performed: {exc}"}
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}
    response, endpoint = await _arequest_troubleshooting(
        "POST", "aps", serial_number, "rebootSwarm", json={}
    )
    if response.status_code not in (200, 201, 202):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return resp_json(response) | {"endpoint_used": endpoint}


# ── AP-Scoped Diagnostics ─────────────────────────────────────────────────────

@mcp.tool(annotations=DIAGNOSTIC)
async def ap_ping(
    serial_number: str,
    destination: str,
    packet_size: int | None = None,
    count: int | None = None,
    interface_port: str | None = None,
    vlan: int | None = None,
    role: str | None = None,
    include_raw_output: bool | None = None,
) -> dict[str, Any]:
    """Ping a destination from an AP and return the result (async, polls ~60s)."""
    payload: dict[str, Any] = {"destination": destination}
    for key, value in (
        ("packetSize", packet_size), ("count", count), ("interfacePort", interface_port),
        ("vlan", vlan), ("role", role), ("includeRawOutput", include_raw_output),
    ):
        if value is not None:
            payload[key] = value
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "ping"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_traceroute(
    serial_number: str,
    destination: str,
    source_interface: str | None = None,
    include_raw_output: bool | None = None,
) -> dict[str, Any]:
    """Run a traceroute from an AP (async, polls ~60s)."""
    payload: dict[str, Any] = {"destination": destination}
    if source_interface is not None:
        payload["sourceInterface"] = source_interface
    if include_raw_output is not None:
        payload["includeRawOutput"] = include_raw_output
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "traceroute"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_tcp(
    serial_number: str,
    host: str,
    port: int,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Test TCP connectivity to host:port from an AP (async, polls ~60s). timeout: 1-10s."""
    payload: dict[str, Any] = {"host": host, "port": port}
    if timeout is not None:
        payload["timeout"] = timeout
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "tcp"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_nslookup(
    serial_number: str,
    host: str,
    dns_server: str | None = None,
) -> dict[str, Any]:
    """Resolve a hostname from an AP's perspective (async, polls ~60s)."""
    payload: dict[str, Any] = {"host": host}
    if dns_server is not None:
        payload["dnsServer"] = dns_server
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "nslookup"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_http(serial_number: str, url: str, timeout: int | None = None) -> dict[str, Any]:
    """Test an HTTP GET from an AP's perspective (async, polls ~60s). timeout: 1-10s."""
    payload: dict[str, Any] = {"url": url}
    if timeout is not None:
        payload["timeout"] = timeout
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "http"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_https(serial_number: str, url: str, timeout: int | None = None) -> dict[str, Any]:
    """Test an HTTPS GET from an AP's perspective (async, polls ~60s). timeout: 1-10s."""
    payload: dict[str, Any] = {"url": url}
    if timeout is not None:
        payload["timeout"] = timeout
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "https"),
        payload,
        errors,
        diagnostic=True,
    )


@mcp.tool(annotations=DIAGNOSTIC)
async def ap_show(serial_number: str, commands: list[str]) -> dict[str, Any]:
    """Run 'show' commands on an AP (all must start with 'show ', max 20, async polls ~60s).

    Fills the AP gap in the existing cx_show/aos_s_show/gateway_show family
    — runapshowcommandsv1 confirms APs now support the same showCommands
    convention.
    """
    if not commands:
        return {"status": None, "errors": ["commands list cannot be empty"]}
    if len(commands) > 20:
        return {"status": None, "errors": [f"commands list cannot exceed 20 items (got {len(commands)})"]}
    for i, cmd in enumerate(commands):
        if not cmd.strip().lower().startswith("show "):
            return {"status": None, "errors": [f"Command {i} must start with 'show ': '{cmd}'"]}
    errors: list[str] = []
    return await atroubleshoot_async(
        get_client(),
        troubleshooting_endpoint_candidates("aps", serial_number, "showCommands"),
        {"commands": commands},
        errors,
        diagnostic=True,
    )


# ── Show-Command Catalog Validation ──────────────────────────────────────────

_SHOW_COMMAND_CATALOG_SEGMENTS = {
    "aps",
    "cx",
    "aos-s",
    "gateways",
}


@mcp.tool(annotations=READ_ONLY)
def list_show_commands(
    serial_number: str,
    device_type: str | None = None,
) -> dict[str, Any]:
    """List the catalog of 'show' commands Central will accept for a device.

    GET .../{platform}/{serial-number}/show-commands — the official
    per-platform allow-list backing cx_show/aos_s_show/gateway_show/ap_show.
    device_type is auto-detected from inventory when omitted (AP/CX/AOS-S/
    Gateway). Use this to validate a command before calling *_show, instead
    of guessing at what's supported.
    """
    dtype = device_type_for_troubleshoot(serial_number, device_type)
    if dtype is None or dtype not in _SHOW_COMMAND_CATALOG_SEGMENTS:
        return {
            "serial_number": serial_number,
            "commands": None,
            "errors": [f"Could not determine a supported platform for {serial_number} "
                       f"(resolved device_type={dtype!r}); pass device_type explicitly."],
        }
    response, endpoint = _request_troubleshooting(
        "GET", dtype, serial_number, "show-commands"
    )
    if response.status_code not in (200, 201, 202):
        return {"serial_number": serial_number, "commands": None,
                "errors": [compact_http_error(response, endpoint)], "endpoint_used": endpoint}
    return {"serial_number": serial_number, "device_type": dtype,
            "commands": resp_json(response), "endpoint_used": endpoint, "errors": []}


# ── Locate Operations ─────────────────────────────────────────────────────────

async def _locate_device(segment: str, serial_number: str) -> dict[str, Any]:
    response, endpoint = await _arequest_troubleshooting(
        "POST",
        segment,
        serial_number,
        "locate",
        diagnostic=True,
        json={},
    )
    if response.status_code not in (200, 201, 202):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return resp_json(response) | {"endpoint_used": endpoint, "serial_number": serial_number}


@mcp.tool(annotations=DIAGNOSTIC)
async def locate_ap(serial_number: str) -> dict[str, Any]:
    """Blink an AP's locate LED (POST .../locate). Non-disruptive — no confirmation required."""
    return await _locate_device("aps", serial_number)


@mcp.tool(annotations=DIAGNOSTIC)
async def locate_cx_switch(serial_number: str) -> dict[str, Any]:
    """Blink a CX switch's locate LED (POST .../locate).

    Non-disruptive — no confirmation required.
    """
    return await _locate_device("cx", serial_number)


@mcp.tool(annotations=DIAGNOSTIC)
async def locate_aos_s_switch(serial_number: str) -> dict[str, Any]:
    """Blink an AOS-S switch's locate LED (POST .../locate).

    Non-disruptive — no confirmation required.
    """
    return await _locate_device("aos-s", serial_number)


# ── Stack-Aware Serial Normalization ─────────────────────────────────────────
#
# Implemented locally (not in shared.py, which only owns device *type*
# resolution via device_type_for_troubleshoot). Best-effort: New Central's
# device-inventory schema for stack member/conductor fields is not fully
# documented in the developer-docs corpus reviewed for this pass, so this
# checks the field names get_switch_stacking_info (monitoring.py) already
# relies on (stackId/switchRole) plus plausible sibling fields, and falls
# back to the given serial unchanged when none are present — it never
# raises and never silently guesses at an unrelated serial.


def _resolve_stack_conductor_serial(serial_number: str) -> str:
    """Return the stack conductor's serial when `serial_number` is a known
    stack member; otherwise return `serial_number` unchanged.

    CX stack troubleshooting/show commands must target the conductor, not
    an individual member. Central's inventory record exposes this via
    switchRole (CONDUCTOR/STANDBY/MEMBER) plus a conductor-serial field —
    field naming for the latter is not independently confirmed, so several
    plausible names are checked defensively.
    """
    device = get_mcp_client().get_device_by_serial(serial_number)
    if not device:
        return serial_number
    role = str(device.get("switchRole") or device.get("stackRole") or "").upper()
    if role in ("", "CONDUCTOR", "STANDALONE", "MASTER"):
        return serial_number
    conductor_serial = (
        device.get("stackConductorSerial")
        or device.get("conductorSerialNumber")
        or device.get("stack_conductor_serial")
    )
    return str(conductor_serial) if conductor_serial else serial_number


@mcp.tool(annotations=READ_ONLY)
def resolve_stack_serial(serial_number: str) -> dict[str, Any]:
    """Resolve a CX switch serial to its stack conductor's serial, if it is a stack member.

    Use before calling cx_ping/cx_traceroute/cx_show/list_show_commands on a
    serial that might belong to a switch stack — those commands execute
    against the stack conductor, not an arbitrary member.
    """
    resolved = _resolve_stack_conductor_serial(serial_number)
    return {
        "serial_number": serial_number,
        "resolved_serial": resolved,
        "was_stack_member": resolved != serial_number,
    }


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )
    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server
    run_server(mcp)
