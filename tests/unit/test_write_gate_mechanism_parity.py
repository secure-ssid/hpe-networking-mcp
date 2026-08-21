"""The three write-gate enforcement mechanisms must refuse the *same set* of tools.

Writes in this repository are denied by default through three independent
mechanisms, each covering a different call path:

1. **Router dispatch** -- ``tool_router._dispatch_tool`` refuses a write/destructive
   call after resolving the backend tool.
2. **Backend dispatcher gate** -- ``shared.install_platform_write_gate`` refuses at
   the tool manager's dispatcher, covering a standalone backend's wire traffic and
   any direct in-process ``server.call_tool(name, ...)``.
3. **Direct-mode registration filtering** -- ``tool_router._register_direct_backend_tools``
   simply does not register a gated-off write, so it is absent rather than refused.

They agree today by inspection, not by construction: (1) classifies capability with
``tool_router._tool_capability`` while (2) uses ``shared.tool_write_capability``, two
deliberately mirrored helpers that nothing forces to stay in step. That is a *silent*
failure mode -- each mechanism has its own tests checked against its own separate
expectation, so the day one side drifts, every existing test still passes and a write
that should refuse starts succeeding.

These tests pin the mechanisms to **each other**. One source of truth (the catalog
below plus the ambient gate configuration), three observations, and an assertion that
all three name the same set. If one dissents the failure says which.

Scope note: this deliberately does *not* depend on the router being exempt from
``install_platform_write_gate`` (``shared.py``'s ``hpe-networking-mcp`` early return).
Mechanism 3 is observed against a freshly constructed target server, so moving or
removing that exemption cannot make these tests silently vacuous.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers import _sdk_compat, shared
from hpe_networking_mcp.mcp_servers import tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    DIAGNOSTIC,
    IDEMPOTENT_WRITE,
    PLATFORM_WRITE_GATE_NAMES,
    READ_ONLY,
    WRITE,
    install_platform_write_gate,
)

#: The single source of truth. Every mechanism is observed against exactly this
#: catalog, so no mechanism carries its own hand-written expectation.
CATALOG: tuple[tuple[str, Any], ...] = (
    ("parity_read", READ_ONLY),
    ("parity_write", WRITE),
    ("parity_idempotent_write", IDEMPOTENT_WRITE),
    ("parity_destructive", DESTRUCTIVE),
    ("parity_diagnostic", DIAGNOSTIC),
)

#: Backend name shapes, derived from the real backend maps rather than hard-coded.
#: Pinning only ``central-config`` would pin the one shape where all three
#: mechanisms already agree, and would miss that they disagree everywhere else.
GATED_SERVER = "central-config"  # -> platform "central", a registered gate

#: An ``_OPTIONAL_SERVER_NAMES`` member whose platform is absent from the gate
#: registry. Router dispatch and the backend gate historically ignored it while
#: direct-mode registration gated it -- one mechanism of three.
UNGATED_OPTIONAL_SERVER = next(
    (
        name
        for name in sorted(router._OPTIONAL_SERVER_NAMES)
        if router._server_platform(name) not in PLATFORM_WRITE_GATE_NAMES
    ),
    None,
)

#: A backend in neither map at all -- the shape any newly added backend has on
#: the day it is added, before anyone remembers the write-gate registry.
UNKNOWN_SERVER = "future-core"

GATE_ENV = (
    "HPE_MCP_ACCESS_PROFILE",
    "HPE_MCP_READONLY",
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_PRODUCT_ACCESS",
    "HPE_MCP_GLP_V2BETA1_WRITES",
)


def _pure_tool(tool_name: str):
    """A tool body with no side effects, so an ungated call proves reachability safely."""

    def _tool() -> dict:
        return {"executed": tool_name}

    _tool.__name__ = tool_name
    return _tool


def _fresh_backend(server: str) -> MCPServer:
    """A backend carrying the catalog, named so it resolves to the shape under test."""
    backend = MCPServer(server)
    for name, annotation in CATALOG:
        backend.add_tool(_pure_tool(name), name=name, annotations=annotation)
    return backend


def _install_catalog(monkeypatch, backend: MCPServer, server: str) -> None:
    """Point the router's resolved-catalog globals at ``backend``."""
    index = {name: _sdk_compat.get_tool(backend, name) for name, _ in CATALOG}
    monkeypatch.setattr(router, "_tool_index", index)
    monkeypatch.setattr(router, "_tool_servers", {n: backend for n in index})
    monkeypatch.setattr(router, "_tool_backend_names", {n: server for n in index})
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)


def _blocked(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("status") == "blocked"


# ── the three observations ───────────────────────────────────────────────────


def _refused_by_router_dispatch(monkeypatch, backend: MCPServer, server: str) -> set[str]:
    _install_catalog(monkeypatch, backend, server)
    refused = set()
    for name, _ in CATALOG:
        result = asyncio.run(router._dispatch_tool(object(), name, {}))
        if _blocked(result):
            refused.add(name)
    return refused


def _refused_by_backend_gate(backend: MCPServer) -> set[str]:
    # Exactly what `shared.run_server` does for a standalone backend.
    install_platform_write_gate(backend)
    refused = set()
    for name, _ in CATALOG:
        result = asyncio.run(backend.call_tool(name, {}))
        payload = json.loads(result.content[0].text)
        if _blocked(payload):
            refused.add(name)
    return refused


def _withheld_by_direct_registration(monkeypatch, backend: MCPServer, server: str) -> set[str]:
    _install_catalog(monkeypatch, backend, server)
    # A *fresh* target, not the module-level router server: mechanism 3 must be
    # observable without relying on the router's write-gate exemption.
    registered = set(router._register_direct_backend_tools(MCPServer("parity-target")))
    return {name for name, _ in CATALOG if name not in registered}


def _observe_all(monkeypatch, server: str = GATED_SERVER) -> dict[str, set[str]]:
    """Every mechanism gets its own backend so one's gate cannot leak into another."""
    return {
        "router_dispatch": _refused_by_router_dispatch(
            monkeypatch, _fresh_backend(server), server
        ),
        "backend_gate": _refused_by_backend_gate(_fresh_backend(server)),
        "direct_registration": _withheld_by_direct_registration(
            monkeypatch, _fresh_backend(server), server
        ),
    }


def assert_mechanisms_agree(observed: dict[str, set[str]]) -> set[str]:
    """Assert all mechanisms name the same tool set; return it.

    On disagreement the message names the dissenting mechanism(s) and, per tool,
    which mechanisms gate it and which let it through -- so the failure points at
    the drift rather than at one arbitrary expectation.
    """
    if len(set(map(frozenset, observed.values()))) == 1:
        return set(next(iter(observed.values())))

    union = set().union(*observed.values())
    consensus = set.intersection(*observed.values())
    contested = sorted(union - consensus)
    majority = Counter(frozenset(s) for s in observed.values()).most_common(1)[0][0]
    dissenting = sorted(n for n, gated in observed.items() if frozenset(gated) != majority)
    detail = "\n".join(
        f"  {tool}: gated by {sorted(m for m, g in observed.items() if tool in g)}; "
        f"NOT gated by {sorted(m for m, g in observed.items() if tool not in g)}"
        for tool in contested
    )
    raise AssertionError(
        "write-gate mechanisms disagree -- a tool gated on one path is reachable on "
        f"another.\ndissenting mechanisms: {dissenting}\n{detail}"
    )


# ── tests ────────────────────────────────────────────────────────────────────


def _backend_shape_cases():
    cases = [pytest.param(GATED_SERVER, id="registered-gate")]
    if UNGATED_OPTIONAL_SERVER is None:
        cases.append(
            pytest.param(
                None,
                id="optional-without-gate",
                marks=pytest.mark.skip(
                    reason="every _OPTIONAL_SERVER_NAMES platform is now in the gate "
                    "registry -- this shape no longer exists, which is the good state"
                ),
            )
        )
    else:
        cases.append(pytest.param(UNGATED_OPTIONAL_SERVER, id="optional-without-gate"))
    cases.append(pytest.param(UNKNOWN_SERVER, id="unknown-backend"))
    return cases


def test_the_two_optional_server_sets_cannot_drift():
    """The gate's optional-product list must match the router's, exactly.

    Tier 2 of the write gate (``shared.OPTIONAL_PRODUCT_SERVER_NAMES``) and the
    router's ``_OPTIONAL_SERVER_NAMES`` are built from different literals in
    different modules -- the router derives its set from ``_OPTIONAL_BACKENDS``,
    which also carries import paths the gate has no business knowing. A product
    added to one and not the other silently changes which refusal a write gets,
    or whether it is refused at all, so pin them rather than trusting review.
    """
    gate_side = set(shared.OPTIONAL_PRODUCT_SERVER_NAMES)
    router_side = set(router._OPTIONAL_SERVER_NAMES)
    assert gate_side == router_side, (
        "shared.OPTIONAL_PRODUCT_SERVER_NAMES and tool_router._OPTIONAL_SERVER_NAMES "
        f"disagree; only in shared: {sorted(gate_side - router_side)}, "
        f"only in tool_router: {sorted(router_side - gate_side)}"
    )


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"HPE_MCP_ACCESS_PROFILE": "safe-read-only"}, id="safe-read-only"),
        pytest.param({"HPE_MCP_READONLY": "1"}, id="aggregate-readonly"),
        pytest.param({}, id="custom-central-deny-by-default"),
        pytest.param({"HPE_MCP_CENTRAL_WRITES": "1"}, id="custom-central-writes-on"),
        pytest.param({"HPE_MCP_ACCESS_PROFILE": "full-read-write"}, id="full-read-write"),
    ],
)
@pytest.mark.parametrize("server", _backend_shape_cases())
def test_all_three_mechanisms_gate_the_same_tools(monkeypatch, env, server):
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    gated = assert_mechanisms_agree(_observe_all(monkeypatch, server))

    # Read and diagnostic capabilities are never gated, on any path.
    assert "parity_read" not in gated
    assert "parity_diagnostic" not in gated


def test_the_agreed_set_is_not_vacuous(monkeypatch):
    """The equivalence must distinguish states, or it would pass while doing nothing.

    One shared expectation, deliberately not three: closed gates withhold every
    write/destructive tool, open gates withhold none.
    """
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
    closed = assert_mechanisms_agree(_observe_all(monkeypatch))
    assert closed == {"parity_write", "parity_idempotent_write", "parity_destructive"}

    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
    assert assert_mechanisms_agree(_observe_all(monkeypatch)) == set()


def test_parity_check_catches_a_one_sided_capability_drift(monkeypatch):
    """The tripwire itself must fail for the right reason, and name the dissenter.

    Induces exactly the divergence this file exists to catch: the backend gate's
    ``shared.tool_write_capability`` and the router's ``tool_router._tool_capability``
    are mirrored by hand, so this misclassifies one destructive tool on the *backend*
    side only. The router and direct registration still gate it; the backend gate
    stops doing so.
    """
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")

    real_capability = shared.tool_write_capability

    def drifted(tool: Any) -> str:
        if getattr(tool, "name", None) == "parity_destructive":
            return "read"
        return real_capability(tool)

    monkeypatch.setattr(shared, "tool_write_capability", drifted)

    with pytest.raises(AssertionError) as excinfo:
        assert_mechanisms_agree(_observe_all(monkeypatch))

    message = str(excinfo.value)
    assert "backend_gate" in message
    assert "parity_destructive" in message
    # The mechanisms that still hold the line must not be reported as dissenting.
    assert "dissenting mechanisms: ['backend_gate']" in message


@pytest.mark.parametrize("server", _backend_shape_cases())
def test_the_mechanisms_agree_for_every_backend_shape(monkeypatch, server):
    """Parity must hold for *every* backend shape, not only for Central.

    The three mechanisms resolve an unregistered platform three different ways:
    router dispatch used to require ``platform in PLATFORM_WRITE_GATE_NAMES``
    before gating at all, the backend gate declined to install when
    ``platform_for_server_name`` returned ``None``, and only direct-mode
    registration gated via the ``_OPTIONAL_SERVER_NAMES`` branch. So an optional
    backend without a registered gate was gated by one mechanism of three, and a
    brand-new backend by none -- ``_write_is_enabled`` terminated in ``return
    True``.

    Deny-by-default means every shape must refuse a write under a closed gate.
    """
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)
    # Deny-by-default *without* an aggregate read-only gate. That distinction is
    # the whole point: under safe-read-only or HPE_MCP_READONLY every mechanism
    # short-circuits on `global_readonly_enabled()` and they agree by accident,
    # which is exactly why the divergence stayed invisible. Here each mechanism
    # must reach its own platform-resolution path to decide.
    monkeypatch.delenv("HPE_MCP_ACCESS_PROFILE", raising=False)

    gated = assert_mechanisms_agree(_observe_all(monkeypatch, server))

    assert gated == {"parity_write", "parity_idempotent_write", "parity_destructive"}, (
        f"backend {server!r}: a write/destructive tool is reachable under "
        f"deny-by-default; only {sorted(gated)} refused"
    )
    assert "parity_read" not in gated
    assert "parity_diagnostic" not in gated


def test_an_unregistered_backend_cannot_be_write_enabled_by_any_setting(monkeypatch):
    """Opening every documented gate must still not open an unregistered backend.

    ``full-read-write`` plus every platform toggle is the most permissive
    configuration the product documents. A backend with no gate entry is not
    reachable by any of them, so there is nothing an operator could set to
    enable it -- which is precisely why allowing it by default was wrong.
    """
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    gated = assert_mechanisms_agree(_observe_all(monkeypatch, UNKNOWN_SERVER))
    assert gated == {"parity_write", "parity_idempotent_write", "parity_destructive"}

    # ...while a registered backend does open, so this is a real distinction and
    # not the whole world being denied.
    assert assert_mechanisms_agree(_observe_all(monkeypatch, GATED_SERVER)) == set()


def test_refusing_an_ungated_backend_never_touches_reads(monkeypatch):
    """The deny-by-default flip must not cost a single read.

    Guards the reason the flip was safe to make: at the time of the change no
    write/destructive tool existed on any backend whose platform is absent from
    the gate registry, and reads short-circuit before any gate is consulted.
    """
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)

    for server in (GATED_SERVER, UNKNOWN_SERVER):
        backend = _fresh_backend(server)
        _install_catalog(monkeypatch, backend, server)
        for readable in ("parity_read", "parity_diagnostic"):
            result = asyncio.run(router._dispatch_tool(object(), readable, {}))
            assert not _blocked(result), f"{server}: {readable} was refused"
            assert result["executed"] == readable
