"""Make the unit suite independent of the developer's ambient ``HPE_MCP_*`` env.

``hpe_networking_mcp.mcp_servers.shared`` calls ``load_dotenv(override=False)``
at import time, so a value the developer's shell or repo-root ``.env`` already
exports **wins** over the file and leaks straight into the test process. That
made the suite report green/red based on who ran it:

* ``HPE_MCP_ACCESS_PROFILE=full-read-write`` + ``HPE_MCP_PRODUCT_ACCESS=read-write``
  flipped ~88 write-gate assertions from "gate denies" to "gate allows".
* ``HPE_MCP_ROUTER_MODE=minimal`` deregistered the router convenience wrappers
  (``find_client``, ``ask_docs``, ...), failing 4 wrapper tests.

Rather than hand-maintaining a list of knobs -- which goes stale the moment
someone adds one -- this module clears the **entire ``HPE_MCP_*`` namespace**
by prefix scan, with a small explicit opt-out set below. A test that needs a
specific value sets it explicitly with ``monkeypatch.setenv``.

Two layers are required, because the knobs are read at two different times:

``_scrub_ambient_hpe_mcp_env()`` at *module import*
    ``tool_router`` reads ``HPE_MCP_ROUTER_MODE`` (module line 88) and builds
    ``_BACKENDS`` (line 514) at **import** time, and the generated-tool
    namespaces register at import too. pytest imports a directory's
    ``conftest.py`` before it collects/imports that directory's test modules,
    so scrubbing here happens before any backend module is imported. A
    function-scoped fixture would run after collection -- far too late.

``_neutralize_ambient_hpe_mcp_env`` autouse fixture
    Restores hermeticity *between* tests for the knobs that are read at call
    time (write gates, access profile, bounding), and undoes anything a
    previous test leaked via bare ``os.environ`` assignment.
"""

from __future__ import annotations

import os

import pytest

from hpe_networking_mcp.mcp_servers.shared import PLATFORM_WRITE_GATE_NAMES

#: Every env var this suite governs shares this prefix. A prefix scan cannot
#: go stale when a new knob is added, which is the whole point.
HPE_MCP_ENV_PREFIX = "HPE_MCP_"

#: The only ``HPE_MCP_*`` variables allowed to survive into a unit test.
#:
#: ``HPE_MCP_ALLOW_PLACEHOLDER_URLS`` is *set* by the autouse fixture in the
#: parent ``tests/conftest.py`` so backend fixtures may use RFC 2606
#: ``*.example.com`` hosts. Parent autouse fixtures run before child ones, so
#: without this opt-out we would delete the value the parent just installed.
#: (``tests/unit/test_placeholder_url_guard.py`` deletes it deliberately when
#: it wants to assert the guard fires.)
PRESERVED_ENV_VARS: frozenset[str] = frozenset(
    {
        "HPE_MCP_ALLOW_PLACEHOLDER_URLS",
    }
)

#: Write-access selectors specifically. Retained as a named export so the
#: write-gate suite can assert the *security-critical* subset is covered even
#: if the prefix scan is ever narrowed.
WRITE_ACCESS_ENV_VARS: tuple[str, ...] = (
    "HPE_MCP_ACCESS_PROFILE",
    "HPE_MCP_PRODUCT_ACCESS",
    "HPE_MCP_READONLY",
    *(f"HPE_MCP_{platform.upper()}_WRITES" for platform in PLATFORM_WRITE_GATE_NAMES),
    # GLP's gate predates the uniform naming convention.
    "HPE_MCP_GLP_V2BETA1_WRITES",
)


def ambient_hpe_mcp_env_names(environ: object = None) -> tuple[str, ...]:
    """``HPE_MCP_*`` names present in ``environ`` that must be scrubbed."""
    source = os.environ if environ is None else environ
    return tuple(
        sorted(
            name
            for name in list(source)
            if name.startswith(HPE_MCP_ENV_PREFIX) and name not in PRESERVED_ENV_VARS
        )
    )


def _scrub_ambient_hpe_mcp_env() -> tuple[str, ...]:
    """Drop inherited ``HPE_MCP_*`` values before test modules are imported."""
    removed = ambient_hpe_mcp_env_names()
    for name in removed:
        os.environ.pop(name, None)
    return removed


#: Executed at conftest import -- i.e. before this directory's test modules
#: (and therefore before ``tool_router`` and the backend servers) are imported.
SCRUBBED_AT_IMPORT: tuple[str, ...] = _scrub_ambient_hpe_mcp_env()


@pytest.fixture(autouse=True)
def _neutralize_ambient_hpe_mcp_env(monkeypatch):
    """Clear inherited ``HPE_MCP_*`` knobs so every test starts from defaults."""
    for name in ambient_hpe_mcp_env_names():
        monkeypatch.delenv(name, raising=False)
    yield
